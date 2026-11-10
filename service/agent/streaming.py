"""Agent 事件 → SSE chunks 格式化"""

import asyncio
import json
import logging
import re
import time
import uuid
from datetime import datetime

from config.settings import settings
from core.agent.middlewares.loop_detect import LoopDetector
from core.agent.middlewares.audit import save_agent_audit_log
from core.agent.chart_spec import assemble_chart_bodies
from core.agent.user_context import set_current_thread_id

logger = logging.getLogger(__name__)


def format_sse(event: str, data: dict) -> str:
  """格式化为 SSE 数据行"""
  payload = json.dumps({'event': event, 'data': data}, ensure_ascii=False)
  return f'data: {payload}\n\n'


_UPLOADS_OPEN = '<current_uploads>'
_UPLOADS_CLOSE = '</current_uploads>'


def _build_current_uploads_block(thread_id: str, uploaded_filenames: list[str] | None) -> str:
  """把本轮消息关联的已上传文件大纲/预览拼成 <current_uploads> 文本块。

  仅覆盖本轮 uploaded_filenames 指定的文件，不包含历史轮次上传过的文件（避免每轮
  重复占用上下文）；提取失败的文件仍列出文件名，附带失败原因，不静默丢弃。
  """
  if not uploaded_filenames:
    return ''

  from core.sandbox import uploads as upload_storage
  from core.sandbox import upload_extract

  parts = []
  for filename in uploaded_filenames:
    path = upload_storage.get_upload_path(thread_id, filename)
    if path is None:
      parts.append(f'### {filename}\n（文件不存在或已失效）')
      continue
    extracted = upload_extract.extract_content(path)
    if extracted.get('error'):
      parts.append(f'### {filename}\n（内容解析失败：{extracted["error"]}）')
      continue
    outline = extracted.get('outline') or []
    outline_text = '\n'.join(f'- {o}' for o in outline) if outline else '（无大纲）'
    preview = extracted.get('text_preview') or ''
    parts.append(f'### {filename}\n大纲：\n{outline_text}\n\n内容摘要：\n{preview}')

  if not parts:
    return ''
  return (
    f'{_UPLOADS_OPEN}\n'
    '以下是用户本轮消息上传的文件内容摘要，请据此理解用户意图并回答：\n\n'
    + '\n\n'.join(parts) +
    f'\n{_UPLOADS_CLOSE}'
  )


_FILE_TAG_RE = re.compile(r'<file\b([^>]*)/>')
_FILE_URL_RE = re.compile(r'url="([^"]*)"')
_FILE_NAME_RE = re.compile(r'name="([^"]*)"')
_ARTIFACT_URL_PREFIX = '/api/v1/chat/agent/artifacts/'
# 其它本就权威的产物前缀，**原样保留不参与反查改写**。对话窗口的讲解视频走独立路由，
# 且落盘的 out.mp4 就在会话目录里 —— 若不放行，`resolve_artifact_url` 会按文件名
# 反查到它、把 URL 改写成 artifacts 路径（拼出来的路径指向 agent-video 子目录，取不到）。
_AUTHORITATIVE_URL_PREFIXES = (_ARTIFACT_URL_PREFIX, '/api/v1/chat/agent-video/')


def _normalize_file_urls(content: str, session_id: str) -> str:
  """把 <file> 标签里 LLM 编造的 url 还原为权威下载 URL（防前端加载失败）。

  仅当 url 非权威前缀且能按文件名反查到真实产物时改写，否则原样保留，
  避免误伤（读路径上不存在的引用就交给前端显式失败）。
  """
  from core.sandbox.datasets import resolve_artifact_url

  thread_id = f'rz-agent-{session_id}'
  set_current_thread_id(thread_id)

  def _fix(m: re.Match) -> str:
    attrs = m.group(1)
    url_m = _FILE_URL_RE.search(attrs)
    name_m = _FILE_NAME_RE.search(attrs)
    if not url_m or not name_m:
      return m.group(0)
    url = url_m.group(1)
    if url.startswith(_AUTHORITATIVE_URL_PREFIXES):
      return m.group(0)
    resolved = resolve_artifact_url(thread_id, name_m.group(1))
    if not resolved:
      return m.group(0)
    return f'<file url="{resolved}" name="{name_m.group(1)}"/>'

  return _FILE_TAG_RE.sub(_fix, content)


def _todos_from_event(data) -> list | None:
  """从 adispatch_custom_event 的载荷里取任务清单（非 todos 事件返回 None）。

  兼容两种包装：payload 直接是 {'todos': [...]}，或 {'input':..,'output':{'todos':..}}。
  """
  if not isinstance(data, dict):
    return None
  payload = data
  if 'todos' not in payload and isinstance(payload.get('output'), dict):
    payload = payload['output']
  todos = payload.get('todos')
  return todos if isinstance(todos, list) else None


# 单组 chart_data（list 形态）在注入侧用的固定组名 —— 模型写 `<chart group="数据"/>` 引用它
SINGLE_GROUP_KEY = '数据'


def _extract_chart_data(result_str: str) -> dict:
  """从 `analysis_exec` 的返回值里取出 `chart_data`，归一成 `{组名: rows}`。

  `chart_data` 两种形态：**dict**（模型把多组放进 `result` dict，键即组名）/
  **list**（单组，套一个固定组名）。

  为什么要有这一层（2026-10-13 事故）：此前模型要把数据**在正文里手写一遍**
  （`<chart ...>[{...}]</chart>`），实测量它会写成 `data="[{...}]"` 这种属性形态 ——
  JSON 里的双引号把标签属性**在第一个引号处截断**，前端 `JSON.parse` 必失败、图全崩。
  根因是"结构化数据过了模型的正文"；`analysis_exec` 本来就返回了 chart_data，
  模型**根本不必重写**。现在正文只留 `<chart group="组名"/>`，数据从这里注入。

  解析失败一律返回空 —— 这只是"给图表补数据"，**不能影响主流程**。
  """
  try:
    payload = json.loads(result_str)
  except Exception:
    return {}
  cd = payload.get('chart_data') if isinstance(payload, dict) else None
  if cd is None:
    return {}
  if isinstance(cd, dict):
    return {str(k): v for k, v in cd.items()}
  return {SINGLE_GROUP_KEY: cd}


async def save_messages(
  session_id: str,
  user_id: str,
  user_content: str,
  assistant_content: str | None = None,
  uploaded_filenames: list[str] | None = None,
  context_tokens: int | None = None,
  todos: list | None = None,
) -> None:
  """落库会话消息（user + 可选 assistant）。

  uploaded_filenames: 本轮用户消息关联的已上传文件名（结构化字段，不进 content 纯文本——
  前端历史消息按 role='user' 直接渲染 content，不会解析标签，塞进文本反而会显示成裸标签）。
  context_tokens: 本轮模型上报的输入 token 数（= 当时上下文大小），挂在 assistant 消息上，
  供前端在打开历史会话时显示「上下文 X%」（Mongo 里的正文是工具结果的 300 字预览，
  不能拿来估算真实上下文）。
  todos: 计划模式 write_todos 最后一次提交的任务清单（结构化字段），挂在 assistant 消息上，
  打开历史会话时还原输入框上方的任务清单托盘。
  """
  from models.rag.message_models import MessageCreate
  from service.rag.conversation.message_service import message_service

  now = datetime.now()
  try:
    # 幂等：前端在开流前已用 POST /sessions/{id}/messages 把这条用户消息落过库
    # （保证"被取消/没跑到 agent"的轮次也留得下提问），这里就不再插第二条。
    if not await _user_message_exists(session_id, user_content):
      await message_service.create_message(MessageCreate(
        session_id=session_id, message_id=uuid.uuid4().hex, role='user',
        content=user_content, user_id=user_id, created_at=now,
        uploaded_filenames=uploaded_filenames,
      ))
    await _autotitle_session(session_id, user_content)
    # 刷新"最近活动"→ 侧栏按 updated_at 排序时这轮会话会浮到最上面（对齐 DeerFlow）
    from service.rag.conversation.session_service import session_service
    await session_service.touch_session(session_id)
    if assistant_content:
      await message_service.create_message(MessageCreate(
        session_id=session_id, message_id=uuid.uuid4().hex, role='assistant',
        content=_normalize_file_urls(assistant_content, session_id),
        user_id=user_id, created_at=now,
        context_tokens=context_tokens,
        todos=todos,
      ))
  except Exception:
    logger.exception('保存 agent 会话消息失败')


# 会话占位名：建会话时不带标题就是这个，改名要一次额外写入（旧实现放在前端 fire-and-forget，
# 失败就永远停在占位名）。侧栏据此判断会话新旧时曾误藏有内容的会话（2026-09 事故）。
PLACEHOLDER_SESSION_NAME = '新对话'


def _make_title(text: str) -> str:
  """取消息前 30 字做标题（口径与前端 chatStore.makeTitle 一致）。"""
  flat = ' '.join((text or '').split())
  return flat[:30] + ('...' if len(flat) > 30 else '')


async def _user_message_exists(session_id: str, content: str) -> bool:
  """该会话**最近一条 user 消息**是否就是这条内容（是 → 前端已落过库，别再插）。

  只看最近一条 user 消息：用户连着问两次同样的话是合法的，中间会隔着 assistant 消息；
  而"前端刚 POST 过、紧接着流结束又落一次"必然是相邻的。查库失败按"不存在"处理
  （宁可多一条重复，也不要漏掉用户的话）。
  """
  try:
    from config.mongodb_conn import mongodb_manager
    doc = await mongodb_manager.db.messages.find_one(
      {'session_id': session_id, 'role': 'user'},
      sort=[('created_at', -1)],
    )
    return bool(doc) and str(doc.get('content') or '') == content
  except Exception:
    logger.exception('查询最近 user 消息失败（按不存在处理） session=%s', session_id)
    return False


async def _autotitle_session(session_id: str, user_content: str) -> None:
  """首次落库用户消息时，把仍是占位名的会话改成「用户那句话」。

  **后端是唯一写入者**：只要用户消息落库了就一定有像样的标题，不依赖前端那次
  可能失败的 PATCH（DeerFlow 的做法也是后端兜底命名，且不需要额外 LLM 调用）。
  改名失败只记日志——标题不阻断落库。
  """
  title = _make_title(user_content)
  if not title:
    return
  try:
    from models.rag.session_models import SessionUpdate
    from service.rag.conversation.session_service import session_service

    session = await session_service.get_session(session_id)
    if session is None or session.session_name != PLACEHOLDER_SESSION_NAME:
      return
    await session_service.update_session(session_id, SessionUpdate(session_name=title))
    logger.info('会话自动命名 session=%s title=%s', session_id, title)
  except Exception:
    logger.exception('会话自动命名失败（不影响落库） session=%s', session_id)


def _is_context_too_long(exc: Exception) -> bool:
  """检测模型 400 上下文超长错误（openai.BadRequestError message）。"""
  msg = str(exc)
  return 'maximum context length' in msg or 'context_length' in msg


def _is_llm_timeout(exc: Exception) -> bool:
  """检测模型流式/响应超时（httpx.ReadTimeout、openai.APITimeoutError 等）。

  含 langchain-openai 的 StreamChunkTimeoutError —— 它的消息是
  `No streaming chunk received for 300.0s (model=…, chunks_received=…)`，
  **不含 "timed out"**，靠消息匹配会漏（它正常走 `except asyncio.TimeoutError`
  分支，这里只是兜住被包装成别的异常类型的情况）。
  """
  msg = str(exc)
  lower = msg.lower()
  return (
    'ReadTimeout' in msg
    or 'APITimeoutError' in msg
    or 'StreamChunkTimeoutError' in msg
    or 'No streaming chunk received' in msg
    or 'Request timed out' in msg
    or 'read timeout' in lower
    or 'timed out' in lower
  )


def _render_error_fallback(header: str, answer_parts: list, tool_calls_log: list) -> str:
  """统一错误兜底文案：失败原因 + 已产出内容提示 + 已查到的工具结果。

  所有错误分支都走这个出口（照搬 DeerFlow `_build_user_fallback_message` 的单一出口
  做法）—— 之前「事件流超时」分支只回一句道歉，整轮查到的工具结果全丢；统一后
  任何错误都不会丢数据。
  """
  lines = [header]
  if answer_parts:
    lines.append('\n以上为部分内容，请稍后重试。')
  if tool_calls_log:
    lines.append('\n已查询到以下结果：')
    for tc in tool_calls_log:
      lines.append(f'【{tc["name"]}】\n{tc.get("result") or "（无结果）"}\n')
  return '\n'.join(lines)


def _is_llm_upstream_error(exc: Exception) -> bool:
  """检测模型上游服务错误（5xx，如 openai.InternalServerError 502/503）。

  网关代理的上游 Qwen 服务瞬时过载/重启时返回 5xx（实测 502）。与 400 类
  参数/上下文错误不同，重试可能恢复——给「稍后重试」的明确提示，区别于
  笼统的「回复生成中断」。
  """
  status = getattr(exc, 'status_code', None) or getattr(exc, 'status', None)
  if isinstance(status, int) and status >= 500:
    return True
  msg = str(exc)
  return 'InternalServerError' in msg or 'Error code: 5' in msg


def _context_usage(tokens: dict) -> dict:
  """上下文用量（done 事件 + 前端「上下文 X%」用）。

  input_tokens 是本轮模型**上报的真实**输入 token（即当时上下文大小）—— 不能用
  Mongo 里的消息正文估算：落库的是工具结果的 300 字预览，远小于真实上下文。
  """
  from core.agent.summarization import trigger_token_budget
  return {
    'input_tokens': tokens.get('input') or 0,
    'output_tokens': tokens.get('output') or 0,
    'window': settings.AGENT_MODEL_MAX_INPUT_TOKENS,
    'compact_trigger': trigger_token_budget(),
  }


class FirstTokenTimeout(asyncio.TimeoutError):
  """本轮开始后首个「有意义事件」迟迟不来（首 token 兜底触发）。

  单独成类是为了和另外两种 TimeoutError 区分开（Python 3.11 起
  asyncio.TimeoutError 就是内置 TimeoutError，只能靠类型分辨）：
  - ``StreamChunkTimeoutError``（langchain-openai）：模型流中途 chunk 间隔超限
  - 裸 ``TimeoutError``：中途静默超过 AGENT_EVENT_TIMEOUT
  - 本类：整轮**一个有意义事件都没有**（模型建了流却不吐 token）
  """


# 计入「有意义事件」的 kind —— 这些都是会真正展示给用户/推进任务的事件。
# 刻意**不含** on_chat_model_start：它在上游刚接流时就发出（实测事故里 2s 就到了），
# 若用它重置首 token 计时，34s 零输出仍会被 360s 的中途静默上限放过，守卫等于失效。
_MEANINGFUL_EVENT_KINDS = frozenset({
  'on_chat_model_stream', 'on_chat_model_end',
  'on_tool_start', 'on_tool_end', 'on_custom_event',
})


async def _iter_agent_events(
  agent, agent_input, config, timeout, last_event_at=None, first_timeout=None,
  ttft_start=None, thread_id='',
):
  """事件级有界迭代：等待下一个事件时设置超时，防整轮挂起（「一直调用中」）。

  每次 anext 用 asyncio.wait_for 兜底 —— 事件流静默超过 timeout 即抛
  asyncio.TimeoutError（由 stream_agent 捕获转友好提示）。正常长流程（工具
  调用 ≤90s、LLM 首 token ≤60s）的事件间隔远小于 timeout，不会误触发。

  last_event_at: 可变容器（如 `[monotonic]`），每收到事件就更新——供异常路径算出
  **真实静默时长**，并据此区分「外层守卫触发」与「内层 LLM chunk 停滞」
  （两者抛的都是 TimeoutError，详情见 stream_agent 的异常分支）。

  first_timeout: 首个「有意义事件」的等待上限（AGENT_FIRST_TOKEN_TIMEOUT）。
  在它到达前用这个更短的窗口（超过即抛 FirstTokenTimeout），到达后回落到
  timeout（中途静默，须容忍分钟级的工具执行）。

  ttft_start / thread_id: 仅用于诊断日志（不改任何行为）。把用户体感的「等了半天
  才出字」拆成两段——① 轮开始 → 首次 on_chat_model_start：编排 + middleware（含
  摘要压缩，实测吃到 3.5s）；② 首次模型调用 → 首个 stream 事件：prefill + 模型思考。
  不拆就分不清该优化哪边：单看一个「16s 的调用」无法判断是 prefill 慢还是生成慢
  （2026-09-28 实测案例）。
  """
  stream = agent.astream_events(agent_input, config=config, version='v2')
  first_seen = False
  model_started_at = None
  while True:
    wait = timeout if (first_seen or not first_timeout) else first_timeout
    try:
      event = await asyncio.wait_for(anext(stream), timeout=wait)
    except StopAsyncIteration:
      return
    except asyncio.TimeoutError as e:
      # 只有 wait_for 自身的超时（类型恰为 TimeoutError）才转成首 token 超时；
      # StreamChunkTimeoutError 等子类原样抛出，保留其自带诊断属性。
      if first_seen or not first_timeout or type(e) is not TimeoutError:
        raise
      raise FirstTokenTimeout(f'首 token 等待超过 {first_timeout}s') from None
    if last_event_at is not None:
      last_event_at[0] = time.monotonic()
    kind = event.get('event', '')
    if kind == 'on_chat_model_start' and model_started_at is None and ttft_start:
      model_started_at = time.monotonic()
      logger.info(
        '首次模型调用 thread=%s 前置耗时=%.2fs（自轮开始：编排+压缩）',
        thread_id, model_started_at - ttft_start,
      )
    if not first_seen and kind in _MEANINGFUL_EVENT_KINDS:
      first_seen = True
      if ttft_start:
        now = time.monotonic()
        model_side = (now - model_started_at) if model_started_at else -1.0
        logger.info(
          '首 token thread=%s ttft=%.2fs 其中模型侧=%.2fs kind=%s',
          thread_id, now - ttft_start, model_side, kind,
        )
    yield event


async def stream_agent(
  agent,
  message: str,
  thread_id: str,
  user_id: str,
  session_id: str,
  initial_state: dict | None = None,
  mode_instruction: str | None = None,
  uploaded_filenames: list[str] | None = None,
  tracing_callbacks: list | None = None,
):
  """执行 Agent 并将事件转为 SSE chunks。

  Args:
    initial_state: 编排层预置的初始 state（如 {'promoted': ...}），与 messages
      合并后传入 agent。为空则仅传 messages（原行为）。
    mode_instruction: 任务模式/开关指令（full_analysis 全量分析、research 调研、
      讲解视频、计划模式任务清单）；非空时拼到本轮 user 消息最前面（可多个叠加）。
    uploaded_filenames: 本轮消息关联的已上传文件名（上传接口返回值）。非空时自动把
      文件大纲/预览摘要拼成 <current_uploads> 块附加到用户消息前，LLM 无需先调
      list_uploaded_files 就能感知本轮上传的内容；历史轮次上传的文件不在此注入，
      靠 list_uploaded_files/read_uploaded_file 工具按需回忆（对齐 DeerFlow
      「当前消息自动可见 + 历史消息工具化检索」的分层设计）。
    tracing_callbacks: 可观测性追踪回调列表（Langfuse，agent_service.py 每请求构造
      一份传入，handler 不跨请求共享）。为空（未启用 Langfuse）时不影响现有行为。

  Yields:
    SSE 格式的字符串
  """
  loop_detector = LoopDetector()
  tool_calls_log: list[dict] = []
  answer_parts: list[str] = []    # 纯文本回答（审计日志用）
  display_parts: list[str] = []   # 含 <tool-call>/<tool-result> 标记（落库用，重现历史工具卡片）
  tokens = {'input': 0, 'output': 0}
  start_time = time.time()
  stop_reason = None
  # 计划模式任务清单：write_todos 每次调用整体替换，只留最后一次用于收尾落库
  latest_todos: list | None = None
  # 本轮累积的图表数据：{组名: rows}。**模型不再在正文里写 JSON**（正文写会被引号截断、图直接崩），
  # 改为 `<chart group="组名"/>` 只报"画哪一组"，数据由这里注入（来源=`analysis_exec` 的返回值，
  # 逐字节准确）。SSE 下发给前端做实时渲染，收尾随消息落库供刷新/历史重放。
  chart_data_all: dict = {}
  logger.info(
    'agent 轮开始 thread=%s mode_inst=%s msg_len=%d',
    thread_id, bool(mode_instruction), len(message),
  )
  turn_start = time.monotonic()   # 诊断用：与下面的 ttft 日志配对

  config = {
    'recursion_limit': settings.AGENT_RECURSION_LIMIT,
    'configurable': {'thread_id': thread_id},
  }
  if tracing_callbacks:
    # 键名必须是 langfuse_session_id / langfuse_user_id：写 session_id / user_id 时
    # Langfuse 认不出，面板「会话」「用户」两列恒为空。
    config['callbacks'] = tracing_callbacks
    config['metadata'] = {
      'langfuse_session_id': session_id,
      'langfuse_user_id': user_id,
      'thread_id': thread_id,
    }
  # 重置 promoted 是纯状态副作用，不带追踪：update_state 产生的 run 没有观测价值，
  # 带上 callbacks 只会多一串噪音 span（早先 132 条 LangGraphUpdateState trace 即来自此）。
  reset_config = {'configurable': config['configurable']}

  # P-1（v1.1）：每请求重置 promoted —— 跨轮累积改为无状态路由（checkpoint 按
  # thread_id 持久化，promoted 带 union reducer 会跨轮越积越多）。
  # 注意不能用 {'promoted': None}：merge_promoted reducer 把空值当「节点未触碰」
  # 保留旧值（实测确认），必须用空 hash 哨兵才能真正清空。当轮内 tool_search →
  # 调用 的累积（merge_promoted union）不受影响；被重置的工具若被调用，middleware
  # 会返回「请先 tool_search」，LLM 可重搜恢复。
  try:
    # AsyncPostgresSaver 不能在主线程调同步 update_state（会抛
    # "Synchronous calls to AsyncPostgresSaver..."），必须用异步版，重置才真正生效。
    await agent.aupdate_state(reset_config, {'promoted': {'catalog_hash': '', 'names': []}})
  except Exception as e:
    # 首次请求无 checkpoint 等边界：promoted 本为空，重置失败不影响本轮
    logger.warning('重置 promoted 失败（忽略，继续本轮）: %s', e)

  # 消息仅传当前用户输入：Checkpointer 通过 add_messages reducer 自动管理跨轮
  # 累积，无需手动从 MongoDB 加载/拼接历史。跨重启持久化由 PostgreSQL Checkpointer
  # 保证（或 MemorySaver 单进程内存）。
  content = message
  if mode_instruction:
    content = f'{mode_instruction}\n\n{content}'
  uploads_block = _build_current_uploads_block(thread_id, uploaded_filenames)
  if uploads_block:
    content = f'{uploads_block}\n\n{content}'
  # 当前时间每请求注入（不烘焙进 Agent 系统提示词——缓存单例里时间会过期）
  now_str = datetime.now().strftime('%Y年%m月%d日 %H:%M')
  content = f'（当前时间：{now_str}）\n\n{content}'
  agent_input = {'messages': [{'role': 'user', 'content': content}]}
  if initial_state:
    agent_input.update(initial_state)

  # 最后一次事件到达时间：异常路径用它算**真实静默时长**，并区分「外层守卫触发」与
  # 「内层 LLM chunk 停滞」（两者都是 TimeoutError，只能靠时长 + 异常类型区分）
  last_event_at = [time.monotonic()]

  try:
    async for event in _iter_agent_events(
      agent, agent_input, config, settings.AGENT_EVENT_TIMEOUT, last_event_at,
      first_timeout=settings.AGENT_FIRST_TOKEN_TIMEOUT,
      ttft_start=turn_start, thread_id=thread_id,
    ):
      kind = event.get('event', '')

      if kind == 'on_chat_model_stream':
        chunk = event.get('data', {}).get('chunk')
        if chunk and hasattr(chunk, 'content') and chunk.content:
          text = chunk.content
          if isinstance(text, str) and text:
            answer_parts.append(text)
            display_parts.append(text)
            yield format_sse('text', {'content': text})

      elif kind == 'on_chat_model_end':
        usage = event.get('data', {}).get('output', {})
        # 断流诊断：正常结束的 OpenAI 兼容流式响应必带 usage_metadata（include_usage
        # 结束块）且 finish_reason 非空；两者都缺失 → 响应被 vLLM/代理静默掐断。这里只
        # 记日志（不改变用户可见行为），把模型实际返回（finish_reason/usage/内容预览）
        # 暴露出来供人工判断断流原因。
        rm = usage.response_metadata if hasattr(usage, 'response_metadata') else {}
        has_usage_block = bool(
          (hasattr(usage, 'usage_metadata') and usage.usage_metadata)
          or rm.get('token_usage')
        )
        finish_reason = rm.get('finish_reason')
        content = getattr(usage, 'content', '') or ''
        if not isinstance(content, str):
          content = str(content)
        if not has_usage_block and not finish_reason:
          logger.warning(
            'Agent 模型流疑似被截断 thread=%s finish_reason=%r has_usage=%s '
            'content_len=%d content_preview=%r',
            thread_id, finish_reason, has_usage_block,
            len(content), content[:200],
          )
        else:
          logger.debug(
            'Agent 模型结束 thread=%s finish_reason=%r has_usage=%s content_len=%d',
            thread_id, finish_reason, has_usage_block, len(content),
          )
        if hasattr(usage, 'usage_metadata') and usage.usage_metadata:
          um = usage.usage_metadata
          tokens['input'] = um.get('input_tokens', 0)
          tokens['output'] = um.get('output_tokens', 0)
        elif rm and rm.get('token_usage'):
          tu = rm.get('token_usage', {})
          tokens['input'] = tu.get('prompt_tokens', 0)
          tokens['output'] = tu.get('completion_tokens', 0)

      elif kind == 'on_tool_start':
        tool_name = event.get('name', 'unknown')
        tool_input = event.get('data', {}).get('input', {})

        # 循环检测
        loop_msg = loop_detector.feed(tool_name, tool_input)
        if loop_msg:
          stop_reason = loop_msg
          yield format_sse('error', {'message': loop_msg})
          return

        # RBAC 检查
        from core.agent.middlewares.guardrail import check_tool_permission
        if not check_tool_permission(tool_name):
          msg = f'权限不足：无权使用工具 {tool_name}'
          yield format_sse('error', {'message': msg})
          stop_reason = msg
          return

        tool_calls_log.append({
          'name': tool_name,
          'args': tool_input,
          'time': time.time(),
        })
        # 与前端 chatStore 直播时插入的标记格式一致（JSON.stringify 为无空格紧凑格式）
        display_parts.append(
          f'\n\n<tool-call name="{tool_name}">'
          f'{json.dumps(tool_input, ensure_ascii=False, separators=(",", ":"))}'
          f'</tool-call>\n\n'
        )
        yield format_sse('tool_call', {'name': tool_name, 'args': tool_input})

      elif kind == 'on_tool_end':
        tool_name = event.get('name', 'unknown')
        output = event.get('data', {}).get('output', '')

        # 格式化工具输出为字符串
        result_str = ''
        if isinstance(output, str):
          result_str = output
        elif hasattr(output, 'content'):
          result_str = str(output.content)
        else:
          result_str = str(output)

        # 累积图表数据：analysis_exec 的返回里带 chart_data（dict=多组 / list=单组）。
        # 模型只在正文写 `<chart group="组名"/>`，数据由这里注入 —— 正文里不再出现 JSON。
        if tool_name == 'analysis_exec':
          chart_data_all.update(_extract_chart_data(result_str))

        # 更新最后一条 tool_call 的结果
        for tc in reversed(tool_calls_log):
          if tc['name'] == tool_name and 'result' not in tc:
            tc['result'] = result_str[:500]
            break

        # 结果预览截断到 300 字符，与前端 chatStore 直播时的 preview 一致
        preview = result_str[:300] + ('...' if len(result_str) > 300 else '')
        display_parts.append(
          f'\n\n<tool-result name="{tool_name}">{preview}</tool-result>\n\n'
        )
        yield format_sse('tool_result', {
          'name': tool_name,
          'result': result_str[:2000],
          # 图表数据单独下发：`result` 预览被截断到 2000 字符，前端拿它渲染不出图。
          # 前端按 `<chart group="组名"/>` 从这里取数（无图表数据时为 None，不占带宽）。
          'chart_data': chart_data_all or None,
        })

      elif kind == 'on_custom_event':
        # 工具内 adispatch_custom_event 发出的阶段进度（如沙箱物化/分析），
        # 只直播展示不落库；历史重放时工具卡片靠 <tool-result> 标记显示完成态
        data = event.get('data', {})
        # 计划模式任务清单（write_todos）：广播给前端托盘，并留一份供收尾落库
        # （落库后刷新页面托盘仍在，不再重犯「刷新即失」的老问题）
        todos = _todos_from_event(data)
        if todos is not None:
          latest_todos = todos
          yield format_sse('todos', {'todos': todos})
          continue
        # 实测 event['data'] = payload 直接；兜底兼容 {'input':..,'output':..} 包装
        if isinstance(data, dict) and 'message' not in data and isinstance(data.get('output'), dict):
          data = data['output']
        if isinstance(data, dict) and data.get('message'):
          yield format_sse('status', {
            'tool': data.get('tool', ''),
            'message': data['message'],
          })

  except asyncio.CancelledError:
    # 客户端断开 / 任务被取消（BaseException，except Exception 捕不到）。
    # 对方已走不再发兜底；记日志 + finally 落库/审计，随后让取消自然传播。
    logger.warning('Agent 流被取消 thread_id=%s tools_called=%d', thread_id, len(tool_calls_log))
    stop_reason = 'cancelled'
    raise

  except FirstTokenTimeout as e:
    # 首 token 兜底：本轮一个有意义事件都没来（模型建了流却不吐 token）。
    # 与下面的中途静默超时分开，因为成因和给用户的建议都不同。
    silent_s = max(0.0, time.monotonic() - last_event_at[0]) if last_event_at else -1.0
    logger.error(
      '首 token 超时 thread=%s 静默=%.1fs 上限=%ds msg_len=%d 原文=%s',
      thread_id, silent_s, settings.AGENT_FIRST_TOKEN_TIMEOUT, len(message), str(e)[:200],
    )
    note = _render_error_fallback(
      '⚠️ 模型长时间没有返回内容（上游无响应）。若反复出现，请稍后重试或开启新对话。',
      answer_parts, tool_calls_log,
    )
    yield format_sse('error', {'message': '模型长时间没有返回内容'})
    stop_reason = 'first_token_timeout'
    yield format_sse('text', {'content': note})
    answer_parts.append(note)
    display_parts.append(note)

  except asyncio.TimeoutError as e:
    # 其余两种超时**都是** TimeoutError（Python 3.11 起 asyncio.TimeoutError is TimeoutError），
    # 只能靠子类区分：
    #   a) langchain-openai 的 StreamChunkTimeoutError（上游流停滞：chunk 间隔超过
    #      AGENT_LLM_TIMEOUT）—— 它的 MRO 是 …→TimeoutError，会**先被本 except 捕获**，
    #      而不是落到下面的 except Exception 分支（历史 bug：被当"事件静默"处理，
    #      日志误报配置值、整轮工具结果全丢）
    #   b) 裸 TimeoutError：_iter_agent_events 的事件静默守卫触发
    silent_s = max(0.0, time.monotonic() - last_event_at[0]) if last_event_at else -1.0
    is_stream_stall = (
      type(e).__name__ == 'StreamChunkTimeoutError'
      or (silent_s >= 0 and silent_s < settings.AGENT_EVENT_TIMEOUT * 0.9)
    )
    if is_stream_stall:
      # 诊断关键：StreamChunkTimeoutError 自带结构化属性（库文档明说不用正则解析消息）
      logger.error(
        'LLM 流停滞（上游长时间无 chunk）thread=%s 距上一事件=%.1fs 异常=%s '
        '上游停滞=%ss model=%s 已收chunk=%s tools_called=%d answer_len=%d 原文=%s',
        thread_id, silent_s, type(e).__name__,
        getattr(e, 'timeout_s', '?'), getattr(e, 'model_name', '?'),
        getattr(e, 'chunks_received', '?'),
        len(tool_calls_log), len(''.join(answer_parts)), str(e)[:300],
      )
      header = ('⚠️ 模型流式响应被中断（上游长时间没有返回数据）。'
                '若反复出现，请让助手把任务拆小一些再试。')
      yield format_sse('error', {'message': '模型流式响应中断'})
      stop_reason = 'llm_stream_stalled'
    else:
      logger.error(
        'Agent 事件流静默超时 thread=%s 静默=%.1fs 上限=%ds tools_called=%d',
        thread_id, silent_s, settings.AGENT_EVENT_TIMEOUT, len(tool_calls_log),
      )
      header = '⚠️ 本次查询处理超时（长时间没有新进展）。'
      yield format_sse('error', {'message': '处理超时'})
      stop_reason = 'event_timeout'
    note = _render_error_fallback(header, answer_parts, tool_calls_log)
    yield format_sse('text', {'content': note})
    answer_parts.append(note)
    display_parts.append(note)

  except Exception as e:
    # 优雅降级（借鉴 DeerFlow 状态契约）：不再把原始异常抛给用户，按状态给可见回应
    # 诊断：异常路径记下已产出的部分内容 + 异常详情，供人工判断断流原因
    logger.exception(
      'Agent stream error thread=%s answer_len=%d answer_preview=%r',
      thread_id,
      len(''.join(answer_parts)),
      ''.join(answer_parts)[:200],
    )
    stop_reason = str(e)
    if _is_context_too_long(e):
      # 对话历史超长（模型 400）：给可操作提示，不静默失败；已有工具结果一并列出
      note = _render_error_fallback(
        '\n\n当前对话历史过长，超出模型上下文上限，无法继续处理。\n'
        '请点击「开启新对话」或删除本会话后重试。',
        [], tool_calls_log,
      )
      yield format_sse('error', {'message': '对话历史过长，请开启新对话'})
    elif _is_llm_timeout(e):
      # LLM 响应超时（重报表场景大上下文推理慢，chunk 间隔 > AGENT_LLM_TIMEOUT）。
      # 专门提示区别于笼统的「回复生成中断」；已有工具结果一并列出不丢数据。
      note = _render_error_fallback(
        '⚠️ AI 处理超时（模型响应较慢）。', answer_parts, tool_calls_log,
      )
      yield format_sse('error', {'message': 'AI 处理超时'})
    elif _is_llm_upstream_error(e):
      # 模型上游 5xx（实测 502）：瞬时过载/重启，重试可能恢复。已查到数据时一并
      # 列出不丢；未出结果则明确提示服务暂不可用。
      status = getattr(e, 'status_code', None)
      status_txt = f'（HTTP {status}）' if isinstance(status, int) else ''
      note = _render_error_fallback(
        f'⚠️ 模型服务暂时不可用{status_txt}。', answer_parts, tool_calls_log,
      )
      yield format_sse('error', {'message': '模型服务暂时不可用，请稍后重试'})
    elif answer_parts:
      note = _render_error_fallback('⚠️ 回复生成中断。', answer_parts, tool_calls_log)
      yield format_sse('error', {'message': '回复生成中断'})
    elif tool_calls_log:
      # 已查到数据但回复生成失败：把工具结果用对话形式给用户，数据不丢
      note = _render_error_fallback(
        '已查询到以下结果，但 AI 生成回复时出错，请重试或换个问法：',
        [], tool_calls_log,
      )
      yield format_sse('error', {'message': '已查询到数据，但回复生成失败'})
    else:
      note = '\n抱歉，本次查询处理失败，请稍后重试或换个问法。'
      yield format_sse('error', {'message': '查询处理失败'})
    yield format_sse('text', {'content': note})
    answer_parts.append(note)
    display_parts.append(note)

  finally:
    duration_ms = int((time.time() - start_time) * 1000)

    # 每轮 Agent INFO 日志（进 stdout → Loki，便于排查；放最前，取消时也记录）
    logger.info(
      'agent 轮结束 thread=%s tools=%d duration=%dms stop=%s',
      thread_id, len(tool_calls_log), duration_ms, stop_reason or 'normal',
    )

    # 先落库、再发 done —— 前端收到 done 即刷新会话列表，若 save 在 done 之后才执行，
    # 用户立刻点回会话时消息还没落库会返回空（「点好几次才出来」）；生成器被取消时
    # done-yield 抛 CancelledError 直接传播，save 更会被整体跳过导致会话永久空。
    # 故把落库放到 done 之前，并包 try/except 兜底（落库失败仅记日志，不影响流）。
    final_answer = ''.join(answer_parts)
    display_answer = ''.join(display_parts) or final_answer
    # 落库前把图表数据由**服务端**写回标签体（恢复「数据与正文同源」，刷新/重开不再断图）。
    # 只装配要落库的这份正文，不碰 SSE 流与审计用的 final_answer；未命中的组名告警，
    # 否则图表会「静默没数据」。
    saved_answer, missed_charts = assemble_chart_bodies(display_answer, chart_data_all)
    if missed_charts:
      logger.warning('图表装配未命中组名 thread=%s groups=%s', thread_id, missed_charts)
    try:
      await save_messages(
        session_id, user_id, message, saved_answer, uploaded_filenames,
        context_tokens=tokens['input'] or None,
        todos=latest_todos,
      )
    except Exception:
      logger.exception('agent 收尾落库失败')

    # 写入审计日志（fire-and-forget）。
    # 注意：函数内不能再出现局部 `import asyncio` —— 那会把 asyncio 变成函数局部
    # 变量，遮蔽模块级导入，导致本函数里 `except asyncio.TimeoutError` 在超时路径
    # 求值即抛 UnboundLocalError（本次线上即此原因整轮失败）。用模块级 import。
    if final_answer or tool_calls_log:
      asyncio.ensure_future(save_agent_audit_log(
        user_id=user_id,
        session_id=session_id,
        question=message,
        answer=final_answer,
        tool_calls=tool_calls_log,
        tokens_used=tokens,
        duration_ms=duration_ms,
        stopped_by_guard=stop_reason,
      ))

    # 发送结束事件（生成器被关闭/取消时 yield 会抛 GeneratorExit/CancelledError，
    # 拦截避免 RuntimeError；取消则让异常继续传播——此时消息已落库，不影响）。
    try:
      yield format_sse('done', {
        'session_id': session_id,
        'thread_id': thread_id,
        'duration_ms': duration_ms,
        'tool_calls_count': len(tool_calls_log),
        # 上下文用量：input_tokens = 本轮模型上报的真实输入（即当时上下文大小），
        # compact_trigger = 压缩触发阈值 —— 前端据此显示「上下文 X%（阈值 32k）」
        'usage': _context_usage(tokens),
      })
    except GeneratorExit:
      raise
    except asyncio.CancelledError:
      raise
    except Exception:
      pass
