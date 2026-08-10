"""Agent 事件 → SSE chunks 格式化"""

import json
import logging
import time

from core.agent.middlewares.loop_detect import LoopDetector
from core.agent.middlewares.audit import save_agent_audit_log

logger = logging.getLogger(__name__)


def format_sse(event: str, data: dict) -> str:
  """格式化为 SSE 数据行"""
  payload = json.dumps({'event': event, 'data': data}, ensure_ascii=False)
  return f'data: {payload}\n\n'


async def stream_agent(
  agent,
  message: str,
  thread_id: str,
  user_id: str,
  session_id: str,
  initial_state: dict | None = None,
):
  """执行 Agent 并将事件转为 SSE chunks。

  Args:
    initial_state: 编排层预置的初始 state（如 {'promoted': ...}），与 messages
      合并后传入 agent。为空则仅传 messages（原行为）。

  Yields:
    SSE 格式的字符串
  """
  loop_detector = LoopDetector()
  tool_calls_log: list[dict] = []
  answer_parts: list[str] = []
  tokens = {'input': 0, 'output': 0}
  start_time = time.time()
  stop_reason = None

  config = {'configurable': {'thread_id': thread_id}}

  # 合并编排层预置 state（如预 promote 的工具），messages 始终存在
  agent_input = {'messages': [{'role': 'user', 'content': message}]}
  if initial_state:
    agent_input.update(initial_state)

  try:
    async for event in agent.astream_events(
      agent_input,
      config=config,
      version='v2',
    ):
      kind = event.get('event', '')

      if kind == 'on_chat_model_stream':
        chunk = event.get('data', {}).get('chunk')
        if chunk and hasattr(chunk, 'content') and chunk.content:
          text = chunk.content
          if isinstance(text, str) and text:
            answer_parts.append(text)
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

        yield format_sse('tool_result', {
          'name': tool_name,
          'result': result_str[:2000],
        })

  except Exception as e:
    logger.exception('Agent stream error')
    yield format_sse('error', {'message': f'Agent 运行异常: {str(e)}'})
    stop_reason = str(e)

  finally:
    duration_ms = int((time.time() - start_time) * 1000)

    # 发送结束事件
    yield format_sse('done', {
      'session_id': session_id,
      'thread_id': thread_id,
      'duration_ms': duration_ms,
      'tool_calls_count': len(tool_calls_log),
    })

    # 写入审计日志（fire-and-forget）
    final_answer = ''.join(answer_parts)
    if final_answer or tool_calls_log:
      import asyncio
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
