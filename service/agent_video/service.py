"""讲解视频的**共用入口**：Agent 工具与 HTTP 端点都走这里，避免两套逻辑分叉。

为什么要有这层：对话里点「讲解视频」按钮走 HTTP 端点、模型自己决定做视频走 Agent 工具，
两条入口的参数校验、会话归属、任务创建、后台启动必须完全一致；否则很快出现
"按钮能做的模型做不了 / 报错文案两套"这类漂移。
"""

import asyncio
import logging
from pathlib import Path

from config.settings import settings

from . import pipeline, slides, store, text_pages

logger = logging.getLogger(__name__)

# 后台任务强引用（asyncio 只持弱引用，丢了会被 GC）
_BACKGROUND: set = set()

PPT_EXTS = ('.ppt', '.pptx')


class VideoRequestError(Exception):
  """入参/环境不满足，可直接回给用户的错误（端点转 4xx、工具转 error JSON）。"""


def _norm_name(name: str) -> str:
  """文件名归一化匹配用：去空白（含全角空格）、全角括号转半角、小写。

  为什么需要：模型复述文件名时会**插空格/改全半角**（实测「副本伊顿 UPS（93PR）…」
  vs 真实「副本伊顿UPS（93PR）…」），按原名查必然找不到——而这不是用户的错。
  """
  s = str(name or '').replace('\u3000', '').replace(' ', '')
  for a, b in (('（', '('), ('）', ')'), ('Ｕ', 'u'), ('Ｐ', 'p')):
    s = s.replace(a, b)
  return s.lower()


def resolve_ppt(thread_id: str, filename: str) -> str:
  """把文件名解析成会话上传目录里的真实路径；失败抛 VideoRequestError（文案可直接给用户）。

  匹配顺序：精确 → 归一化（去空格/全半角/大小写）→ **本会话唯一的 PPT 兜底**。
  最后一档是给"模型把文件名说错"用的：会话里只有一份 PPT 时，用户意图不可能有歧义。
  """
  from core.sandbox import uploads

  if not filename:
    raise VideoRequestError('请提供 PPT 文件名（先在对话里把 PPT 作为附件上传）')

  path = uploads.get_upload_path(thread_id, filename)
  if path is None:
    ppts = [
      f for f in uploads.list_uploads(thread_id)
      if Path(f.get('filename', '')).suffix.lower() in PPT_EXTS
    ]
    want = _norm_name(filename)
    for f in ppts:
      if _norm_name(f.get('filename', '')) == want:
        path = uploads.get_upload_path(thread_id, f['filename'])
        logger.info('文件名不完全一致，按归一化匹配到 %r（请求 %r）', f['filename'], filename)
        break
    if path is None and len(ppts) == 1:
      path = uploads.get_upload_path(thread_id, ppts[0]['filename'])
      logger.info('本会话只有一份 PPT，按它兜底：%r（请求 %r）', ppts[0]['filename'], filename)

  if path is None:
    available = [f.get('filename', '') for f in uploads.list_uploads(thread_id)]
    raise VideoRequestError(
      f'未找到文件 {filename!r}。当前会话已上传：{available or "（无）"}。'
      f'（文件名请**逐字复制**，不要加空格或改写）'
    )
  if path.suffix.lower() not in PPT_EXTS:
    raise VideoRequestError(f'{filename!r} 不是 PowerPoint 文件（需 .ppt/.pptx）')
  return str(path)



def _write_outline(thread_id: str, content: str) -> str:
  """把文字/大纲落到会话目录下的 outline.md（与页图同目录，便于追溯与重跑）。"""
  from core.sandbox import datasets as sandbox_datasets
  base = sandbox_datasets.thread_dir(thread_id) / 'agent-video'
  base.mkdir(parents=True, exist_ok=True)
  path = base / f'outline-{new_task_id()}.md'
  path.write_text(content, encoding='utf-8')
  return str(path)


def new_task_id() -> str:
  from .store import new_task_id as _new  # 单一实现，避免两处生成规则漂移
  return _new()


async def ensure_ready() -> None:
  """环境自检：缺 soffice、或 LO 渲染不出汉字时，给出明确提示（而不是产出残缺成品）。"""
  if not slides.soffice_available():
    raise VideoRequestError(
      '服务器未安装 LibreOffice（soffice），无法渲染 PPT 页面；请联系管理员'
    )
  if not await slides.cjk_render_available():
    raise VideoRequestError(
      '运行环境缺少 LibreOffice 可用的中文字体（渲染出来会没有汉字）：'
      '请在部署镜像里安装 fonts-noto-cjk 后重启，或联系管理员'
    )


async def create_task(
  *,
  thread_id: str,
  user_id: str,
  session_id: str,
  filename: str = '',
  language: str = '中文',
  voice: str = 'neutral',
  aspect_ratio: str = '16:9',
  motion: str = '',
  content: str = '',
  title: str = '',
  subtitle_language: str = '',
  subtitles: bool = True,
  target_minutes: float = 0,
  requirements: str = '',
) -> str:
  """校验入参 → 建任务 → 起后台执行（不等结果）；返回 task_id。

  调用方（Agent 工具）随后用 `wait_for_task()` 等它跑完，把成品链接放进本轮回复。

  Args:
    filename: 会话上传目录里的 PPT 文件名（**必填**：工具与模型都不能猜，用户上传后由
      `list_uploaded_files` 或前端附件给出）。
    language/voice/aspect_ratio: 配音语言 / 音色 / 画幅，均有默认值。
    subtitle_language: 字幕语言（支持中文/英文）；空或与 `language` 同语言时字幕沿用配音稿。
    subtitles: 本条视频是否烧字幕（用户确认时说"不要字幕"就传 False）。
    target_minutes: 目标总时长（分钟，0=不限）；换算成每页解说词字数上限，**只能近似控制**。
    requirements: 用户附加要求（受众/语气/片尾安全提示等），拼进解说词提示词。
  """
  # 两条输入形态共用同一套下游（解说词 → TTS → 合成）：
  #   'ppt'  —— 会话里上传的 PPT（需 soffice 渲染）
  #   'text' —— 用户直接给的文字/大纲（本地 Pillow 排版，不依赖 soffice）
  if content and not filename:
    source_kind, title = 'text', (title or '讲解视频')
    # 文字路径不依赖 soffice，但依赖 Pillow 用的中文字体文件（缺了整片汉字变方框）
    try:
      text_pages.require_font()
    except text_pages.CjkFontError as e:
      raise VideoRequestError(str(e)) from e
    outline_path = _write_outline(thread_id, content)
    ppt_path = ''
  else:
    source_kind, outline_path = 'ppt', ''
    # 先校验用户输入（文件在不在、是不是 PPT），再校验环境（soffice/ffmpeg）——
    # 否则用户拿到的是"服务器没装 LibreOffice"这种他无法自己解决的提示，掩盖了真实问题
    ppt_path = resolve_ppt(thread_id, filename)
    await ensure_ready()          # 含中文字体探针（缺字体直接给出可操作原因）

  # 页面数上限在 slides.py 里拦；这里先给出"已存在同名在跑"之类的重复创建保护
  running = [
    t for t in await store.list_tasks(thread_id, limit=10)
    if t.get('status') not in (store.STATUS_SUCCESS, store.STATUS_FAILED)
  ]
  if running:
    logger.info('本会话已有讲解视频任务在跑，仍按用户意图新建: %s', running[0]['task_id'])

  task_id = await store.create_task(
    thread_id=thread_id, user_id=user_id, session_id=session_id, ppt_path=ppt_path,
    language=language or '中文', voice=voice or 'neutral', aspect_ratio=aspect_ratio,
    motion=motion or settings.VIDEO_AGENT_MOTION, source_kind=source_kind,
    title=title, outline_path=outline_path, subtitle_language=subtitle_language or '',
    subtitles=subtitles, target_minutes=target_minutes, requirements=requirements or '',
  )
  start_background(pipeline.run_task(task_id))
  logger.info('讲解视频任务已创建: task=%s ppt=%s', task_id, Path(ppt_path).name)
  return task_id


# 播放链接的签名 token（Redis，短期）：key 前缀与飞书产物 token 分开，互不干扰
_VIDEO_URL_KEY = 'agent:video:url:'

# 等任务跑完时的轮询间隔（秒）：也是往对话里推进度的节奏——**必须远小于看门狗**
# （AGENT_EVENT_TIMEOUT=360）与 nginx 的 proxy_read_timeout（300s），否则长任务会把流掐断
WAIT_POLL_SECONDS = 4.0


async def wait_for_task(
  task_id: str, *, timeout: float = 0, on_progress=None,
) -> tuple[dict, bool]:
  """等任务跑到终态；返回 `(任务快照, 是否超时)`。

  为什么要等（而不是像以前那样立刻返回 task_id）：用户在对话里要的是"这个视频"，早返回会让
  本轮回复与成品脱钩——得靠前端另外补一张卡片，多个任务并存时就分不清哪个对应哪次请求。

  Args:
    timeout: 等待上限（秒）；<=0 取 `settings.VIDEO_AGENT_WAIT_TIMEOUT`。超时不取消任务，
      只是停止等待并如实告诉用户"还在跑"（前端会继续盯）。
    on_progress: `async (task) -> None`，每次拿到新快照时调用（用于推进度事件）。
  """
  limit = float(timeout or settings.VIDEO_AGENT_WAIT_TIMEOUT)
  deadline = asyncio.get_running_loop().time() + limit
  last: dict = await store.get_task(task_id) or {}
  while True:
    if last.get('status') in (store.STATUS_SUCCESS, store.STATUS_FAILED):
      return last, False
    if asyncio.get_running_loop().time() >= deadline:
      logger.info('等待讲解视频任务超时（%.0fs），转为后台继续: task=%s status=%s',
                  limit, task_id, last.get('status'))
      return last, True
    if on_progress:
      try:
        await on_progress(last)
      except Exception as e:  # noqa: BLE001 - 进度只是反馈，绝不能影响任务
        logger.debug('推进度事件失败: %s', e)
    await asyncio.sleep(WAIT_POLL_SECONDS)
    last = await store.get_task(task_id) or last


def progress_text(task: dict) -> str:
  """给用户看的一行进度（如"合成视频 第 3/10 页（85%）"）。"""
  return f"{task.get('progress_text') or task.get('status') or '处理中'}（{task.get('progress', 0)}%）"


def sign_video_url(task_id: str, ttl: int | None = None) -> str:
  """给任务的播放地址签名，返回**可直接给 <video src> 用**的相对 URL。

  为什么不用 `?token=<用户 JWT>`：真 JWT 会进 nginx 访问日志与浏览器历史，而且只有
  30 分钟有效期——20 分钟的视频看到后半段拖进度就 401 了。这里发的是 Redis 里的
  短期随机 token（token 即授权、TTL VIDEO_URL_TOKEN_TTL），与飞书产物下载同一思路。
  """
  import secrets
  from config.redis_conn import redis_manager
  token = secrets.token_urlsafe(24)
  try:
    redis_manager.client.set(
      redis_manager.key(f'{_VIDEO_URL_KEY}{token}'), task_id,
      ex=ttl or settings.VIDEO_URL_TOKEN_TTL,
    )
  except Exception as e:  # noqa: BLE001 - Redis 不可用时退回无签名 URL（端点仍走 Bearer 鉴权）
    logger.warning('签名播放链接失败（退回 Bearer 鉴权路径）: %s', e)
    return f'/api/v1/chat/agent-video/{task_id}/video'
  return f'/api/v1/chat/agent-video/{task_id}/video?t={token}'


def resolve_video_url_token(token: str) -> str:
  """签名 token → task_id；无效/过期返回空串。"""
  from config.redis_conn import redis_manager
  if not token:
    return ''
  try:
    return redis_manager.client.get(redis_manager.key(f'{_VIDEO_URL_KEY}{token}')) or ''
  except Exception as e:  # noqa: BLE001
    logger.warning('校验播放链接签名失败: %s', e)
    return ''


async def list_session_tasks(thread_id: str, limit: int = 20) -> list[dict]:
  """本会话的讲解视频任务（前端自动盯进度/刷新后补卡片用）。

  只回必要字段，够前端渲染进度与视频卡片即可；含 `video_url`（完成后才有值）。
  """
  tasks = await store.list_tasks(thread_id, limit=limit)
  out = []
  for t in tasks:
    out.append({
      'task_id': t['task_id'],
      'status': t['status'],
      'progress': t.get('progress', 0),
      'progress_text': t.get('progress_text', ''),
      'source_name': t.get('source_name', ''),
      'page_count': len(t.get('pages') or []),
      'duration': t.get('duration', 0),
      'error': t.get('error', ''),
      'video_url': (sign_video_url(t['task_id'])
                    if t['status'] == store.STATUS_SUCCESS else ''),
      'created_at': t['created_at'].isoformat() if t.get('created_at') else '',
    })
  return out


def start_background(coro) -> None:
  task = asyncio.create_task(coro)
  _BACKGROUND.add(task)
  task.add_done_callback(_BACKGROUND.discard)


