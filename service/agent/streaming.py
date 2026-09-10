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


def _normalize_file_urls(content: str, session_id: str) -> str:
  """把 <file> 标签里 LLM 编造的 url 还原为权威下载 URL（防前端加载失败）。

  仅当 url 非权威前缀且能按文件名反查到真实产物时改写，否则原样保留，
  避免误伤（读路径上不存在的引用就交给前端显式失败）。
  """
  from core.sandbox.datasets import resolve_artifact_url

  thread_id = f'rz-agent-{session_id}'

  def _fix(m: re.Match) -> str:
    attrs = m.group(1)
    url_m = _FILE_URL_RE.search(attrs)
    name_m = _FILE_NAME_RE.search(attrs)
    if not url_m or not name_m:
      return m.group(0)
    url = url_m.group(1)
    if url.startswith(_ARTIFACT_URL_PREFIX):
      return m.group(0)
    resolved = resolve_artifact_url(thread_id, name_m.group(1))
    if not resolved:
      return m.group(0)
    return f'<file url="{resolved}" name="{name_m.group(1)}"/>'

  return _FILE_TAG_RE.sub(_fix, content)


def render_plan_instruction(plan: dict | None) -> str:
  """把已确认的执行计划渲染成「执行指令」消息文本。

  plan 为前端回传的编辑后计划 dict：{'goal': str, 'steps': [{'title','description'}, ...]}。
  返回的文本作为本轮 user 消息喂给 LLM，让 create_agent 首轮即按计划执行。
  """
  plan = plan or {}
  goal = plan.get('goal') or ''
  steps = plan.get('steps') or []
  lines = [
    '以下是已确认的执行计划，请严格按步骤执行（不要再输出计划、不要反问，直接开始执行）：',
  ]
  if goal:
    lines.append(f'目标：{goal}')
  for i, step in enumerate(steps, 1):
    if not isinstance(step, dict):
      continue
    title = step.get('title') or ''
    desc = step.get('description') or ''
    lines.append(f'{i}. {title}：{desc}')
  return '\n'.join(lines)


async def save_messages(
  session_id: str,
  user_id: str,
  user_content: str,
  assistant_content: str | None = None,
  uploaded_filenames: list[str] | None = None,
) -> None:
  """落库会话消息（user + 可选 assistant），供计划轮与执行轮共用。

  uploaded_filenames: 本轮用户消息关联的已上传文件名（结构化字段，不进 content 纯文本——
  前端历史消息按 role='user' 直接渲染 content，不会解析标签，塞进文本反而会显示成裸标签）。
  """
  from models.rag.message_models import MessageCreate
  from service.rag.conversation.message_service import message_service

  now = datetime.now()
  try:
    await message_service.create_message(MessageCreate(
      session_id=session_id, message_id=uuid.uuid4().hex, role='user',
      content=user_content, user_id=user_id, created_at=now,
      uploaded_filenames=uploaded_filenames,
    ))
    if assistant_content:
      await message_service.create_message(MessageCreate(
        session_id=session_id, message_id=uuid.uuid4().hex, role='assistant',
        content=_normalize_file_urls(assistant_content, session_id),
        user_id=user_id, created_at=now,
      ))
  except Exception:
    logger.exception('保存 agent 会话消息失败')


def _is_context_too_long(exc: Exception) -> bool:
  """检测模型 400 上下文超长错误（openai.BadRequestError message）。"""
  msg = str(exc)
  return 'maximum context length' in msg or 'context_length' in msg


def _is_llm_timeout(exc: Exception) -> bool:
  """检测模型流式/响应超时（httpx.ReadTimeout、openai.APITimeoutError 等）。"""
  msg = str(exc)
  lower = msg.lower()
  return (
    'ReadTimeout' in msg
    or 'APITimeoutError' in msg
    or 'Request timed out' in msg
    or 'read timeout' in lower
    or 'timed out' in lower
  )


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


async def _iter_agent_events(agent, agent_input, config, timeout):
  """事件级有界迭代：等待下一个事件时设置超时，防整轮挂起（「一直调用中」）。

  每次 anext 用 asyncio.wait_for 兜底 —— 事件流静默超过 timeout 即抛
  asyncio.TimeoutError（由 stream_agent 捕获转友好提示）。正常长流程（工具
  调用 ≤90s、LLM 首 token ≤60s）的事件间隔远小于 timeout，不会误触发。
  """
  stream = agent.astream_events(agent_input, config=config, version='v2')
  while True:
    try:
      event = await asyncio.wait_for(anext(stream), timeout=timeout)
    except StopAsyncIteration:
      return
    yield event


async def stream_agent(
  agent,
  message: str,
  thread_id: str,
  user_id: str,
  session_id: str,
  initial_state: dict | None = None,
  plan_instruction: str | None = None,
  mode_instruction: str | None = None,
  uploaded_filenames: list[str] | None = None,
  tracing_callbacks: list | None = None,
):
  """执行 Agent 并将事件转为 SSE chunks。

  Args:
    initial_state: 编排层预置的初始 state（如 {'promoted': ...}），与 messages
      合并后传入 agent。为空则仅传 messages（原行为）。
    plan_instruction: 计划模式确认后的执行指令文本；非空时替换本轮 user 消息
      （落库仍用 message，即前端确认时传的简短文案）。
    mode_instruction: 任务模式指令（如 full_analysis 全量分析）；非空时拼到本轮
      user 消息最前面（与 plan_instruction 可叠加）。
    uploaded_filenames: 本轮消息关联的已上传文件名（上传接口返回值）。非空时自动把
      文件大纲/预览摘要拼成 <current_uploads> 块附加到用户消息前，LLM 无需先调
      list_uploaded_files 就能感知本轮上传的内容；历史轮次上传的文件不在此注入，
      靠 list_uploaded_files/read_uploaded_file 工具按需回忆（对齐 DeerFlow
      「当前消息自动可见 + 历史消息工具化检索」的分层设计）。
    tracing_callbacks: 可观测性追踪回调列表（Langfuse，agent_service.py 构建期初始化
      一次并缓存传入）。为空（未启用 Langfuse）时不影响现有行为。

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
  logger.info(
    'agent 轮开始 thread=%s mode_inst=%s msg_len=%d',
    thread_id, bool(mode_instruction), len(message),
  )

  config = {
    'recursion_limit': settings.AGENT_RECURSION_LIMIT,
    'configurable': {'thread_id': thread_id},
  }
  if tracing_callbacks:
    config['callbacks'] = tracing_callbacks
    config['metadata'] = {
      'session_id': session_id, 'user_id': user_id, 'thread_id': thread_id,
    }

  # P-1（v1.1）：每请求重置 promoted —— 跨轮累积改为无状态路由（checkpoint 按
  # thread_id 持久化，promoted 带 union reducer 会跨轮越积越多）。
  # 注意不能用 {'promoted': None}：merge_promoted reducer 把空值当「节点未触碰」
  # 保留旧值（实测确认），必须用空 hash 哨兵才能真正清空。当轮内 tool_search →
  # 调用 的累积（merge_promoted union）不受影响；被重置的工具若被调用，middleware
  # 会返回「请先 tool_search」，LLM 可重搜恢复。
  try:
    # AsyncPostgresSaver 不能在主线程调同步 update_state（会抛
    # "Synchronous calls to AsyncPostgresSaver..."），必须用异步版，重置才真正生效。
    await agent.aupdate_state(config, {'promoted': {'catalog_hash': '', 'names': []}})
  except Exception as e:
    # 首次请求无 checkpoint 等边界：promoted 本为空，重置失败不影响本轮
    logger.warning('重置 promoted 失败（忽略，继续本轮）: %s', e)

  # 消息仅传当前用户输入：Checkpointer 通过 add_messages reducer 自动管理跨轮
  # 累积，无需手动从 MongoDB 加载/拼接历史。跨重启持久化由 PostgreSQL Checkpointer
  # 保证（或 MemorySaver 单进程内存）。
  # 计划模式确认轮：用渲染后的执行指令替换本轮 user 消息，让 LLM 首轮即按计划执行。
  content = plan_instruction if plan_instruction else message
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

  try:
    async for event in _iter_agent_events(
      agent, agent_input, config, settings.AGENT_EVENT_TIMEOUT,
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
        })

      elif kind == 'on_custom_event':
        # 工具内 adispatch_custom_event 发出的阶段进度（如沙箱物化/分析），
        # 只直播展示不落库；历史重放时工具卡片靠 <tool-result> 标记显示完成态
        data = event.get('data', {})
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

  except asyncio.TimeoutError:
    # 整轮事件流静默超时：给用户可见的友好提示，落入 finally（done + 落库）
    logger.error('Agent 事件流超时（%ss 无事件）', settings.AGENT_EVENT_TIMEOUT)
    note = '\n抱歉，本次查询处理超时，请稍后重试或换个问法。'
    yield format_sse('error', {'message': '处理超时'})
    yield format_sse('text', {'content': note})
    answer_parts.append(note)
    display_parts.append(note)
    stop_reason = 'event_timeout'

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
      note = ('\n\n当前对话历史过长，超出模型上下文上限，无法继续处理。\n'
              '请点击「开启新对话」或删除本会话后重试。')
      if tool_calls_log:
        lines = [note, '\n已查询到以下结果：']
        for tc in tool_calls_log:
          lines.append(f'【{tc["name"]}】\n{tc.get("result") or "（无结果）"}\n')
        note = '\n'.join(lines)
      yield format_sse('error', {'message': '对话历史过长，请开启新对话'})
    elif _is_llm_timeout(e):
      # LLM 响应超时（重报表场景大上下文推理慢，chunk 间隔 > AGENT_LLM_TIMEOUT）。
      # 专门提示区别于笼统的「回复生成中断」；已有工具结果一并列出不丢数据。
      if not answer_parts and tool_calls_log:
        lines = ['⚠️ AI 处理超时（模型响应较慢）。已查询到以下结果：']
        for tc in tool_calls_log:
          lines.append(f'【{tc["name"]}】\n{tc.get("result") or "（无结果）"}\n')
        note = '\n'.join(lines)
      else:
        note = '\n\n⚠️ AI 处理超时（模型响应较慢），以上为部分内容，请稍后重试。'
      yield format_sse('error', {'message': 'AI 处理超时'})
    elif _is_llm_upstream_error(e):
      # 模型上游 5xx（实测 502）：瞬时过载/重启，重试可能恢复。已查到数据时一并
      # 列出不丢；未出结果则明确提示服务暂不可用。
      status = getattr(e, 'status_code', None)
      status_txt = f'（HTTP {status}）' if isinstance(status, int) else ''
      if not answer_parts and tool_calls_log:
        lines = [f'⚠️ 模型服务暂时不可用{status_txt}，请稍后重试。已查询到以下结果：']
        for tc in tool_calls_log:
          lines.append(f'【{tc["name"]}】\n{tc.get("result") or "（无结果）"}\n')
        note = '\n'.join(lines)
      else:
        note = f'\n\n⚠️ 模型服务暂时不可用{status_txt}，以上为部分内容，请稍后重试。'
      yield format_sse('error', {'message': '模型服务暂时不可用，请稍后重试'})
    elif answer_parts:
      note = '\n\n⚠️ 回复生成中断，以上为部分内容，请重试。'
      yield format_sse('error', {'message': '回复生成中断'})
    elif tool_calls_log:
      # 已查到数据但回复生成失败：把工具结果用对话形式给用户，数据不丢
      lines = ['已查询到以下结果，但 AI 生成回复时出错，请重试或换个问法：\n']
      for tc in tool_calls_log:
        lines.append(f'【{tc["name"]}】\n{tc.get("result") or "（无结果）"}\n')
      note = '\n'.join(lines)
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
    try:
      await save_messages(session_id, user_id, message, display_answer, uploaded_filenames)
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
      })
    except GeneratorExit:
      raise
    except asyncio.CancelledError:
      raise
    except Exception:
      pass
