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
) -> None:
  """落库会话消息（user + 可选 assistant），供计划轮与执行轮共用。"""
  from models.rag.message_models import MessageCreate
  from service.rag.conversation.message_service import message_service

  now = datetime.now()
  try:
    await message_service.create_message(MessageCreate(
      session_id=session_id, message_id=uuid.uuid4().hex, role='user',
      content=user_content, user_id=user_id, created_at=now,
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
):
  """执行 Agent 并将事件转为 SSE chunks。

  Args:
    initial_state: 编排层预置的初始 state（如 {'promoted': ...}），与 messages
      合并后传入 agent。为空则仅传 messages（原行为）。
    plan_instruction: 计划模式确认后的执行指令文本；非空时替换本轮 user 消息
      （落库仍用 message，即前端确认时传的简短文案）。

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

  config = {
    'recursion_limit': settings.AGENT_RECURSION_LIMIT,
    'configurable': {'thread_id': thread_id},
  }

  # P-1（v1.1）：每请求重置 promoted —— 跨轮累积改为无状态路由（checkpoint 按
  # thread_id 持久化，promoted 带 union reducer 会跨轮越积越多）。
  # 注意不能用 {'promoted': None}：merge_promoted reducer 把空值当「节点未触碰」
  # 保留旧值（实测确认），必须用空 hash 哨兵才能真正清空。当轮内 tool_search →
  # 调用 的累积（merge_promoted union）不受影响；被重置的工具若被调用，middleware
  # 会返回「请先 tool_search」，LLM 可重搜恢复。
  try:
    agent.update_state(config, {'promoted': {'catalog_hash': '', 'names': []}})
  except Exception as e:
    # 首次请求无 checkpoint 等边界：promoted 本为空，重置失败不影响本轮
    logger.warning('重置 promoted 失败（忽略，继续本轮）: %s', e)

  # 消息仅传当前用户输入：Checkpointer 通过 add_messages reducer 自动管理跨轮
  # 累积，无需手动从 MongoDB 加载/拼接历史。跨重启持久化由 PostgreSQL Checkpointer
  # 保证（或 MemorySaver 单进程内存）。
  # 计划模式确认轮：用渲染后的执行指令替换本轮 user 消息，让 LLM 首轮即按计划执行。
  content = plan_instruction if plan_instruction else message
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
        if hasattr(usage, 'usage_metadata') and usage.usage_metadata:
          um = usage.usage_metadata
          tokens['input'] = um.get('input_tokens', 0)
          tokens['output'] = um.get('output_tokens', 0)
        elif hasattr(usage, 'response_metadata') and usage.response_metadata:
          rm = usage.response_metadata
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
    logger.exception('Agent stream error')
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

    # 发送结束事件
    yield format_sse('done', {
      'session_id': session_id,
      'thread_id': thread_id,
      'duration_ms': duration_ms,
      'tool_calls_count': len(tool_calls_log),
    })

    # 落库会话消息（用户问题 + 助手回答）到 messages 集合，供前端历史加载。
    # 助手回答存 display_parts（含 <tool-call>/<tool-result> 标记，与前端直播插入的
    # 格式一致），重载历史时 ChatArea 会解析标记重现工具卡片；纯文本回答在
    # answer_parts（final_answer），审计日志用。
    final_answer = ''.join(answer_parts)
    display_answer = ''.join(display_parts) or final_answer
    await save_messages(session_id, user_id, message, display_answer)

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
