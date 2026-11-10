"""对话窗口「讲解视频」的 Agent 工具集（纯加法能力，与表单页视频功能互不影响）。

工具形态与 `schedule_task_tool.py` 一致：`@tool` + async + JSON 字符串返回 + **工具内
自行校验权限**。为什么权限要自己查：`core/agent/middlewares/guardrail.py` 的
`check_tool_permission` 只认 `ai:agent*`，不认 `ai:video`，所以按日程工具的做法在
工具里显式判断，避免"有 ai:agent 就能生成视频"。

生成/重跑都要几分钟（PPT 渲染 + N 页解说词 + N 页 TTS + 合成），但**工具会等它跑完再返回**
（`service.wait_for_task`，上限 `VIDEO_AGENT_WAIT_TIMEOUT`）：这样本轮回复里就能直接带上
`<file>` 视频卡片，用户不用猜"哪个视频对应哪次请求"。等待期间每 4 秒
`adispatch_custom_event` 推一次进度（前端实时显示），也顺便让事件流不静默——
否则会被 `AGENT_EVENT_TIMEOUT`(360s) 与 nginx 的 300s 读超时掐断。

超时（默认 20 分钟）不取消任务：返回 `finished=false`，交给前端轮询补卡片。
`video_status` / `video_pages` 仍用于事后查询与逐页改文案。
"""

import asyncio
import json
import logging
from typing import Optional

from langchain.tools import tool
from langchain_core.runnables.config import RunnableConfig
from pydantic import BaseModel, Field

from config.settings import settings

logger = logging.getLogger(__name__)

# 允许的画幅与音色（与旧视频页可选项保持一致；language 自由填，如 中文/英文/English）
_ASPECT_RATIOS = ('16:9', '9:16', '1:1')
# 后台任务引用：保持强引用防被 GC 回收（asyncio 只持弱引用）
_BACKGROUND: set = set()


def _err(msg: str) -> str:
  return json.dumps({'error': msg}, ensure_ascii=False)


def _require_video_permission() -> str | None:
  """返回错误 JSON 字符串；None 表示通过。"""
  from core.agent.middlewares.guardrail import get_current_permissions
  if 'ai:video' not in get_current_permissions():
    return _err('无权限：生成视频需要 ai:video 权限')
  return None


def _current_thread(config: Optional[RunnableConfig] = None) -> str:
  """当前会话 thread_id：优先 contextvar（由 AgentService.stream 设置），
  为空时回退 LangChain config 的 configurable.thread_id（照 schedule_task_tool 的取法）。

  为什么要有回退：工具执行期若 contextvar 没设（历史 bug），会拿不到会话 → 既报错又丢失
  会话归属校验。
  """
  from core.agent.user_context import get_current_thread_id
  thread_id = get_current_thread_id()
  if thread_id:
    return thread_id
  if config:
    return ((config.get('configurable') or {}).get('thread_id') or '')
  return ''


def _progress_cb(tool: str):
  """给指定工具造一个进度回调（推 SSE `status` 事件，实时显示在**该工具的卡片**上）。

  走 `adispatch_custom_event`（与分析工具同一套通道）：等待期几分钟必须有事件流出去，
  否则会被流式看门狗（AGENT_EVENT_TIMEOUT）和 nginx 的读超时掐断。纯反馈，失败不影响任务。

  `tool` 必须是**调用方自己的工具名**：前端 `ToolCard` 用 `toolStatus[name]` 取进度，
  名字写错（原来一律写 'video_create'）会让 `video_update_page`/`video_rerender`
  的卡片**看不到任何进度**。
  """
  async def _emit(task: dict) -> None:
    try:
      from langchain_core.callbacks import adispatch_custom_event
      from service.agent_video import service as video_service
      await adispatch_custom_event('agent_video_status', {
        'tool': tool,
        'message': f'正在生成视频：{video_service.progress_text(task)}',
      })
    except Exception:  # noqa: BLE001 - 进度只是反馈
      pass
  return _emit


async def _finish_payload(task_id: str, timed_out: bool, extra: dict) -> str:
  """等到（或等超）之后统一组装工具返回值。

  - 完成 → 带上**签名播放链接**，让模型当场输出 `<file>`（本轮回复就带上视频，不靠前端另补卡片）
  - 失败 → 如实带上任务错误
  - 超时 → 如实说明"还在后台跑"，前端会继续盯进度并补卡片
  """
  from service.agent_video import service as video_service
  from service.agent_video import store

  task = await store.get_task(task_id) or {}
  status = task.get('status', '')
  if status == store.STATUS_SUCCESS:
    return json.dumps({
      'ok': True, 'task_id': task_id, 'status': status, 'finished': True,
      'ppt_filename': await task_source_name(task_id),
      'video_url': video_service.sign_video_url(task_id),
      'next': '本工具已等到视频完成：**立刻**在回复里输出一行 '
              '`<file url="上面 video_url" name="<文件名>.mp4"/>`（url 原样复制），'
              '再配一两句说明即可，不要说"稍后给链接"',
      **extra,
    }, ensure_ascii=False)
  if status == store.STATUS_FAILED:
    return json.dumps({
      'ok': False, 'task_id': task_id, 'status': status, 'finished': True,
      'error': task.get('error', '未知错误'),
      'next': '如实把错误告诉用户，不要演绎成平台故障',
      **extra,
    }, ensure_ascii=False)
  return json.dumps({
    'ok': True, 'task_id': task_id, 'status': status, 'finished': False,
    'progress': video_service.progress_text(task),
    'message': '视频还在后台生成（本次等待已超时），完成后卡片会自动出现在对话里',
    'next': '告诉用户还在生成、完成后视频会自己出现；他要问进度就用 video_status 查',
    **extra,
  }, ensure_ascii=False)


def _start_background(coro) -> None:
  """起后台任务并保持强引用。"""
  task = asyncio.create_task(coro)
  _BACKGROUND.add(task)
  task.add_done_callback(_BACKGROUND.discard)


def _resolve_ppt(thread_id: str, filename: str) -> tuple[str, str]:
  """把用户给的文件名解析成上传目录里的真实路径（与 HTTP 端点共用同一实现）。

  Returns:
    (绝对路径, 错误信息)；成功时错误信息为空串。
  """
  from service.agent_video import service as video_service

  try:
    return video_service.resolve_ppt(thread_id, filename), ''
  except video_service.VideoRequestError as e:
    return '', str(e)


@tool
async def video_create(
  ppt_filename: str,
  language: str = '中文',
  voice: str = 'neutral',
  aspect_ratio: str = '16:9',
  motion: str = '',
  subtitle_language: str = '',
  show_subtitles: bool = True,
  target_minutes: float = 0,
  requirements: str = '',
  config: Optional[RunnableConfig] = None,
) -> str:
  """用会话里已上传的 PPT 生成一段讲解视频（每页配解说词与配音，输出 MP4）。

  **本工具会等到视频生成完再返回**（渲染页面 → 逐页写解说词 → 逐页配音 → 合成，通常几分钟）：
  返回的 `video_url` 就是成品链接，收到后**立刻**在回复里输出 `<file url="…"/>`。
  等待期间用户能看到实时进度。只有超过 20 分钟仍没跑完才会返回 `finished=false`（任务继续在
  后台跑，卡片稍后自己出现）。之后用 video_status 查进度、video_pages 看逐页文案。

  Args:
    ppt_filename: 会话里已上传的 PPT 文件名（如 汇报.pptx）。
    language: 配音语言，默认中文（支持中文/英文）。
    voice: 音色，neutral（默认）/ male / female。
    aspect_ratio: 画幅，16:9（默认）/ 9:16 / 1:1。
    motion: 画面运镜，默认 none（静态，画面最清晰）；用户明确想要动效时才传
      mixed（按页轮换推近/平移）或 zoom_in/zoom_out/pan_left/pan_right。
    subtitle_language: 字幕语言，**只在用户要求"字幕与配音不同语言"时传**（如配音英文、
      字幕中文就传「中文」）；不传则字幕与配音同语言。解说词会默认烧成画面底部字幕。
    show_subtitles: 要不要字幕，默认 True；用户明确说"不要字幕"时传 False。
      （注意别和 `subtitle_language` 混了：这个只管开关，语言用那个。）
    target_minutes: 目标时长（分钟），用户确认时说"约 5 分钟"就传 5；**只能近似控制**
      （换算成每页解说词字数上限），成片实际时长由配音决定，交付时要如实报实际时长。
    requirements: 用户确认时提的其它要求原文（受众/语气/片尾安全提示等），会拼进解说词提示词。
  """
  denied = _require_video_permission()
  if denied:
    return denied

  from core.agent.user_context import get_current_user_id
  from service.agent_video import service as video_service

  thread_id = _current_thread(config)
  user_id = get_current_user_id() or ''
  if not thread_id:
    return _err('没能识别当前会话，请重新发送一条消息再试')

  aspect = aspect_ratio if aspect_ratio in _ASPECT_RATIOS else '16:9'
  session_id = (config.get('configurable', {}) or {}).get('thread_id', '') if config else ''
  session_id = session_id.replace('rz-agent-', '') if session_id else thread_id

  try:
    task_id = await video_service.create_task(
      thread_id=thread_id, user_id=user_id, session_id=session_id,
      filename=ppt_filename, language=language or '中文',
      voice=voice or 'neutral', aspect_ratio=aspect, motion=motion or '',
      subtitle_language=subtitle_language or '', subtitles=show_subtitles,
      target_minutes=target_minutes, requirements=requirements or '',
    )
  except video_service.VideoRequestError as e:
    return _err(str(e))

  # 等它跑完（等待期推实时进度）；超时则退回"后台跑 + 前端补卡片"
  task, timed_out = await video_service.wait_for_task(
    task_id, on_progress=_progress_cb('video_create'))
  # 回报**实际使用**的文件名：模型复述常与真实名不完全一致（多空格/全半角），
  # resolve_ppt 会容错匹配，这里把真名给回去，避免回复里再写错
  return await _finish_payload(task_id, timed_out, {
    'ppt_filename': await task_source_name(task_id),
    'language': language, 'voice': voice, 'aspect_ratio': aspect,
    'subtitle_language': subtitle_language or language,
  })


@tool
async def video_create_from_text(
  title: str,
  content: str,
  language: str = '中文',
  voice: str = 'neutral',
  aspect_ratio: str = '16:9',
  motion: str = '',
  subtitle_language: str = '',
  show_subtitles: bool = True,
  target_minutes: float = 0,
  requirements: str = '',
) -> str:
  """把一段文字/大纲直接做成讲解视频（用户没有 PPT、只贴了文字时用这个）。

  文字会由平台本地排版成页图（无需上传文件），之后与 PPT 路径完全一致：
  逐页写解说词 → 配音 → 合成 MP4，并支持改某页/换语言后只重跑该页。
  **与 video_create 一样会等到生成完再返回**（等待期用户能看到实时进度）。

  Args:
    title: 视频标题（会做成封面页的大标题）。
    content: 完整文字或大纲；支持 Markdown 标题分页，也可用 `---` 显式分页。
    language: 配音语言，默认中文（支持中文/英文）。
    voice: 音色，neutral（默认）/ male / female。
    aspect_ratio: 画幅，16:9（默认）/ 9:16 / 1:1。
    motion: 画面运镜，默认 none（静态）；用户明确想要动效时才传 mixed 等。
    subtitle_language: 字幕语言；只在用户要求"字幕与配音不同语言"时传，不传即同配音语言。
    show_subtitles: 要不要字幕，默认 True；用户明确说"不要字幕"时传 False。
      （注意别和 `subtitle_language` 混了：这个只管开关，语言用那个。）
    target_minutes: 目标时长（分钟），**只能近似控制**（折算每页解说词字数上限）。
    requirements: 用户确认时提的其它要求原文（受众/语气/片尾提示等）。
  """
  denied = _require_video_permission()
  if denied:
    return denied

  from core.agent.user_context import get_current_user_id
  from service.agent_video import service as video_service

  thread_id = _current_thread()
  if not thread_id:
    return _err('没能识别当前会话，请重新发送一条消息再试')
  if not (content or '').strip():
    return _err('content 为空：请把要讲的内容/大纲传进来（支持 Markdown 标题分页）')

  aspect = aspect_ratio if aspect_ratio in _ASPECT_RATIOS else '16:9'
  try:
    task_id = await video_service.create_task(
      thread_id=thread_id, user_id=get_current_user_id() or '', session_id=thread_id,
      content=content, title=title or '视频生成', language=language or '中文',
      voice=voice or 'neutral', aspect_ratio=aspect, motion=motion or '',
      subtitle_language=subtitle_language or '', subtitles=show_subtitles,
      target_minutes=target_minutes, requirements=requirements or '',
    )
  except video_service.VideoRequestError as e:
    return _err(str(e))
  except Exception as e:  # noqa: BLE001 - 例如大纲页数超上限
    return _err(f'文字转视频失败：{e}')

  task, timed_out = await video_service.wait_for_task(
    task_id, on_progress=_progress_cb('video_create_from_text'))
  return await _finish_payload(task_id, timed_out, {
    'title': title or '视频生成',
    'language': language, 'voice': voice, 'aspect_ratio': aspect,
  })


@tool
async def video_status(task_id: str) -> str:
  """查看视频生成任务的进度、每页状态与最终视频地址。

  Args:
    task_id: video_create 返回的任务 ID。
  """
  denied = _require_video_permission()
  if denied:
    return denied

  from service.agent_video import service as video_service
  from service.agent_video import store

  task = await store.get_task(task_id, thread_id=_current_thread())
  if not task:
    return _err(f'未找到任务 {task_id}（或不属于当前会话）')

  pages = task.get('pages') or []
  return json.dumps({
    'ok': True,
    'task_id': task_id,
    'status': task['status'],
    'progress': task.get('progress', 0),
    'progress_text': task.get('progress_text', ''),
    'language': task.get('language', ''),
    'subtitle_language': task.get('subtitle_language') or task.get('language', ''),
    # 实际成片时长（秒）与目标时长：让模型能如实报"实际 4 分 52 秒（目标 5 分钟）"，
    # 而不是把目标时长当承诺（目标只能近似控制，TTS 语速决定实际值）
    'duration_seconds': round(float(task.get('duration') or 0), 1),
    'target_minutes': task.get('target_minutes') or 0,
    'subtitles': task.get('subtitles', True),
    'page_count': len(pages),
    'pages': [{'index': p['index'], 'status': p.get('status'),
               'has_script': bool(p.get('script')), 'duration': p.get('duration', 0),
               'error': p.get('error', '')} for p in pages],
    # 用**签名**链接（Redis 短期 token，与 task_id 绑定）：模型把 <file url> 输出给前端后，
    # <video src> 直接播、原生下载也能用，不必把用户 JWT 放进 URL
    'video_url': (video_service.sign_video_url(task_id)
                  if task['status'] == store.STATUS_SUCCESS else ''),
    'error': task.get('error', ''),
  }, ensure_ascii=False)


@tool
async def video_pages(task_id: str, page_no: Optional[int] = None) -> str:
  """查看视频生成任务的逐页解说词（用户要改某一页时，先看这里）。

  Args:
    task_id: video_create 返回的任务 ID。
    page_no: 只看某一页（1 起）；不传则列出全部页。
  """
  denied = _require_video_permission()
  if denied:
    return denied

  from service.agent_video import store

  task = await store.get_task(task_id, thread_id=_current_thread())
  if not task:
    return _err(f'未找到任务 {task_id}（或不属于当前会话）')

  pages = task.get('pages') or []
  if page_no is not None:
    pages = [p for p in pages if p.get('index') == page_no]
    if not pages:
      return _err(f'第 {page_no} 页不存在（该任务共 {len(task.get("pages") or [])} 页）')
  return json.dumps({
    'ok': True, 'task_id': task_id, 'page_count': len(task.get('pages') or []),
    # subtitle 只在**与配音稿不同**时才给（双语任务），避免同语言时整段重复一遍
    'pages': [{'index': p['index'], 'script': p.get('script', ''),
               **({'subtitle': p['subtitle']} if (p.get('subtitle') and
                                                  p['subtitle'] != p.get('script')) else {}),
               'status': p.get('status'), 'check': p.get('checks', {})} for p in pages],
  }, ensure_ascii=False)


@tool
async def video_update_page(task_id: str, page_no: int, script: str) -> str:
  """改写**一页**的解说词并只重跑该页（配音 + 该页片段 + 重新合成），其余页不动。

  **要改多页（≥2 页）请改用 `video_update_pages` 一次传完**：这个工具一次只能一页，
  而同一个工具**累计调用 20 次会被安全闸中止**（防死循环，见
  `core/agent/middlewares/loop_detect.py` 的 `TOOL_FREQ_HARD`）——50 页逐页改必然
  改到一半被掐断，而且每页都要重新 concat 一次整条视频。

  Args:
    task_id: video_create 返回的任务 ID。
    page_no: 要改的页号（1 起，见 video_pages）。
    script: 新的解说词全文（不是"改动说明"；直接给完整文案）。
  """
  denied = _require_video_permission()
  if denied:
    return denied

  from service.agent_video import store

  task = await store.get_task(task_id, thread_id=_current_thread())
  if not task:
    return _err(f'未找到任务 {task_id}（或不属于当前会话）')
  if task.get('intermediates_pruned'):
    return _err('这条视频的中间产物已按保留策略清理（只保留了成片），无法只重跑单页；'
                '请重新生成一条新视频')
  if not (script or '').strip():
    return _err('新解说词不能为空')

  return await _apply_scripts(task_id, {int(page_no): script.strip()}, tool='video_update_page')


class PageScript(BaseModel):
  """批量改页的单项：页号 + 该页新解说词全文。"""

  page_no: int = Field(..., description='页号（1 起，见 video_pages）')
  script: str = Field(..., description='该页新的解说词全文（不是"改动说明"）')


@tool
async def video_update_pages(task_id: str, pages: list[PageScript]) -> str:
  """**一次批量改写多页**解说词（配音 + 重编这些页 + 只重新合成一次），其余页不动。

  **改多页一律用这个**，不要逐页调 `video_update_page`：
  - 同一个工具累计调用 20 次会被安全闸中止（`core/agent/middlewares/loop_detect.py` 的
    `TOOL_FREQ_HARD`）→ 50 页逐页改必然改到一半被掐断
  - 逐页还会**每页都重新 concat 一次整条视频**，批量只 concat 一次

  Args:
    task_id: video_create 返回的任务 ID。
    pages: 要改的页，每项 `{"page_no": 3, "script": "完整新文案"}`；一次最多 60 页。
  """
  denied = _require_video_permission()
  if denied:
    return denied

  from service.agent_video import store

  task = await store.get_task(task_id, thread_id=_current_thread())
  if not task:
    return _err(f'未找到任务 {task_id}（或不属于当前会话）')
  if task.get('intermediates_pruned'):
    return _err('这条视频的中间产物已按保留策略清理（只保留了成片），无法只重跑部分页；'
                '请重新生成一条新视频')
  if not pages:
    return _err('pages 为空：请传 [{"page_no": 3, "script": "…"}]')

  total = len(task.get('pages') or [])
  if len(pages) > settings.VIDEO_AGENT_MAX_PAGES:
    return _err(f'一次最多改 {settings.VIDEO_AGENT_MAX_PAGES} 页，收到 {len(pages)} 页')

  # 先整体校验再动手：缺页 / 空文案就说清楚，避免"改了一半"
  problems: list[str] = []
  scripts: dict[int, str] = {}
  for item in pages:
    if not (item.script or '').strip():
      problems.append(f'第 {item.page_no} 页文案为空')
      continue
    if not (1 <= item.page_no <= total):
      problems.append(f'第 {item.page_no} 页不存在（该任务共 {total} 页）')
      continue
    scripts[int(item.page_no)] = item.script.strip()
  if problems:
    return _err('；'.join(problems))
  if not scripts:
    return _err('没有可应用的页')

  return await _apply_scripts(task_id, scripts, tool='video_update_pages')


async def _apply_scripts(task_id: str, scripts: dict[int, str], *, tool: str) -> str:
  """把 {页号: 新文案} 落到任务上并**等它重跑完**，返回工具结果 JSON（单页/多页共用）。

  - **一定要批量传**：逐页调会把同一工具累计次数推到 20 次上限被中止（见 video_update_page 说明）
  - 重跑只做一次 concat（`_compose_all(reuse=True)` 只重编改动页），N 页也只等一轮
  """
  from service.agent_video import pipeline, store
  from service.agent_video import service as video_service

  # **起后台之前先把任务状态拨离 success**：重跑的头一小段里任务还挂着上一轮的 success，
  # 前端轮询会据此补卡片并开始播——而成片正在被重写，读到的就是半成品（实测加载失败）。
  # 这里同步改状态，窗口为零。
  await store.set_status(task_id, store.STATUS_TTS, progress=55,
                         progress_text=f'准备重跑 {len(scripts)} 页…')
  # 后台重跑（即便本轮对话断开，重跑也照跑完），然后**等它跑完再回话**：
  # 与 video_create 同一口径，回复里直接带上新视频，不让用户自己猜哪次改的
  _start_background(_rerun_with_scripts(task_id, scripts, pipeline))
  _, timed_out = await video_service.wait_for_task(task_id, on_progress=_progress_cb(tool))
  return await _finish_payload(task_id, timed_out,
                               {'page_nos': sorted(scripts)} if len(scripts) > 1
                               else {'page_no': next(iter(scripts))})


async def _rerun_with_scripts(task_id: str, scripts: dict[int, str], pipeline) -> None:
  """用**指定文案**重跑若干页（不走 LLM 重写，保证原文被原样采用），最后只 concat 一次。"""
  from service.agent_video import compose, narration, store
  try:
    task = await store.get_task(task_id)
    work = store.work_dir(task['thread_id'], task_id)
    pages = task.get('pages') or []
    dual = narration.needs_dual(task.get('language', ''), task.get('subtitle_language', ''))
    target: list[dict] = []
    for page in pages:
      script = scripts.get(page['index'])
      if script is None:
        continue
      # 配音语言 ≠ 字幕语言时，新文案不能直接当字幕用 → 换算一份（失败就沿用新文案）
      subtitle = ''
      if dual:
        subtitle = await narration.translate_text(script, task.get('subtitle_language', ''))
      page.update({'script': script, 'subtitle': subtitle or script,
                   'status': store.PAGE_PENDING, 'error': ''})
      await store.update_one_page(task_id, page['index'], {
        'script': page['script'], 'subtitle': page['subtitle'],
        'status': store.PAGE_PENDING, 'error': '',
      })
      target.append(page)
    if not target:
      return
    await pipeline._synthesize_pages(
      task_id, target, work, task['voice'], progress_base=60,
      speech_language=narration.speech_language(task.get('language', '')),
    )
    await store.set_pages(task_id, pages)
    # motion 必须带上任务自己的：不带就按全局默认重编，这一页会跟其它页**动效不一致**
    video = await pipeline._compose_all(task_id, pages, work, task['aspect_ratio'], reuse=True,
                                       motion=task.get('motion', ''),
                                       subtitles=task.get('subtitles', True))
    await store.set_video(task_id, str(video), duration=compose.total_duration(pages))
  except Exception as e:  # noqa: BLE001 - 后台任务自收尾
    logger.exception('按指定文案重跑 %d 页失败: task=%s', len(scripts), task_id)
    await store.mark_failed(task_id, f'按指定文案重跑失败: {e}')


@tool
async def video_set_options(
  task_id: str,
  language: Optional[str] = None,
  voice: Optional[str] = None,
  subtitle_language: Optional[str] = None,
) -> str:
  """修改视频生成任务的配音语言/音色/字幕语言（作用于之后重跑的页面；生效需再调 video_rerender）。

  Args:
    task_id: video_create 返回的任务 ID。
    language: 新的配音语言（如 英文）；不传则不改。
    voice: 新音色（neutral / male / female）；不传则不改。
    subtitle_language: 新字幕语言；不传则不改。填成与配音语言相同即"字幕跟配音一致"。
  """
  denied = _require_video_permission()
  if denied:
    return denied
  if not language and not voice and not subtitle_language:
    return _err('请至少指定 language / voice / subtitle_language 之一')

  from service.agent_video import store

  task = await store.get_task(task_id, thread_id=_current_thread())
  if not task:
    return _err(f'未找到任务 {task_id}（或不属于当前会话）')
  await store.set_options(task_id, language=language, voice=voice,
                          subtitle_language=subtitle_language)
  return json.dumps({
    'ok': True, 'task_id': task_id, 'language': language or task['language'],
    'voice': voice or task['voice'],
    'subtitle_language': subtitle_language or task.get('subtitle_language') or task['language'],
    'message': '选项已更新；调用 video_rerender 重跑后生效',
  }, ensure_ascii=False)


@tool
async def video_rerender(task_id: str, page_nos: Optional[str] = None) -> str:
  """按当前选项重跑页面（重写解说词 + 重新配音 + 重新合成）。默认全部页。

  **同样会等到重跑完再返回**（返回的 video_url 就是新成品，收到后立刻输出 `<file …/>`）。

  用途：换语言/音色后生效；或修复 video_status 里标了 needs_review/degraded 的页。

  Args:
    task_id: video_create 返回的任务 ID。
    page_nos: 要重跑的页号，逗号分隔（如 "3,5"）；不传则全部页。
  """
  denied = _require_video_permission()
  if denied:
    return denied

  from service.agent_video import pipeline, store

  task = await store.get_task(task_id, thread_id=_current_thread())
  if not task:
    return _err(f'未找到任务 {task_id}（或不属于当前会话）')
  if task.get('intermediates_pruned'):
    return _err('这条视频的中间产物已按保留策略清理（只保留了成片），无法只重跑部分页；'
                '请重新生成一条新视频')
  if not (task.get('pages') or []):
    return _err('该任务还没有逐页产物，请等待首次生成完成')

  targets = None
  if page_nos:
    try:
      targets = [int(x) for x in str(page_nos).replace('，', ',').split(',') if x.strip()]
    except ValueError:
      return _err(f'page_nos 需要是逗号分隔的数字，收到 {page_nos!r}')

  # 同为"重跑"：起后台前先同步把状态拨离 success（理由见 video_update_page 处注释）
  await store.set_status(task_id, store.STATUS_TTS, progress=55,
                         progress_text=f'准备重跑 {len(targets) if targets else "全部"} 页…')
  # 后台跑 + 等它跑完（口径同 video_create）：重跑完直接在回复里带上新视频
  from service.agent_video import service as video_service
  _start_background(_rerun_safe(task_id, targets, pipeline, store))
  _, timed_out = await video_service.wait_for_task(
    task_id, on_progress=_progress_cb('video_rerender'))
  return await _finish_payload(task_id, timed_out, {'pages': targets or 'all'})


async def _rerun_safe(task_id: str, targets, pipeline, store) -> None:
  try:
    await pipeline.rerun_pages(task_id, targets)
  except Exception as e:  # noqa: BLE001
    logger.exception('重跑讲解视频失败: task=%s', task_id)
    await store.mark_failed(task_id, f'重跑失败: {e}')


async def task_source_name(task_id: str) -> str:
  """任务记录里的原始文件名（给工具回报用；取不到返回空串）。"""
  from service.agent_video import store
  try:
    task = await store.get_task(task_id)
    return (task or {}).get('source_name', '')
  except Exception:  # noqa: BLE001
    return ''


def build_agent_video_tools() -> list:
  """返回可注入 agent 的讲解视频工具（由 agent_service 按开关注册）。"""
  from core.agent.middlewares.tool_error import wrap_async_tool_with_error_handling
  return [
    wrap_async_tool_with_error_handling(t)
    for t in (video_create, video_create_from_text, video_status, video_pages,
              video_update_page, video_update_pages, video_set_options, video_rerender)
  ]
