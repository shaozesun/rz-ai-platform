"""工具异常处理 — 捕获工具异常返回友好消息，Agent 不崩溃"""

import json
import functools
import logging

logger = logging.getLogger(__name__)


def wrap_tool_with_error_handling(tool):
  """包装同步工具：异常时返回友好错误消息而非崩溃"""
  original_func = tool.func

  @functools.wraps(original_func)
  def safe_func(*args, **kwargs):
    try:
      return original_func(*args, **kwargs)
    except Exception as e:
      logger.warning('工具调用失败 tool=%s error=%s', tool.name, str(e))
      return json.dumps({
        'error': f'工具 "{tool.name}" 执行失败: {str(e)}',
        'suggestion': '请尝试其他方式查询，或联系管理员检查后端服务状态',
      }, ensure_ascii=False)

  # 创建同 schema 的新工具
  from langchain_core.tools import StructuredTool
  return StructuredTool.from_function(
    func=safe_func,
    name=tool.name,
    description=tool.description,
    args_schema=tool.args_schema,
  )


def wrap_async_tool_with_error_handling(tool):
  """包装 async 工具：异常时返回错误 JSON 而非抛出。

  区别于 wrap_tool_with_error_handling（只适用同步工具）：同步 wrapper 包 async
  工具会返回未 await 的 coroutine，且 try/except 捕获不到 await 后才抛出的异常，
  异常会一路穿透中断整个 Agent 流。本函数 await 原 coroutine，把异常转成错误串
  返回——喂回 LLM 后它能在 ReAct 循环里观察错误并自我纠正（换参数/换能力重试），
  而不是整轮「回复生成中断」。供 analysis 工具等 raw_tools 通道使用（deferred /
  返回 Command 的工具不能包，见 build.py 说明）。
  """
  original_coroutine = tool.coroutine

  @functools.wraps(original_coroutine)
  async def safe_coroutine(*args, **kwargs):
    try:
      return await original_coroutine(*args, **kwargs)
    except Exception as e:
      logger.warning('工具调用失败 tool=%s error=%s', tool.name, str(e))
      return json.dumps({
        'error': f'工具 "{tool.name}" 执行失败: {str(e)}',
        'suggestion': '请根据错误调整参数或改用其他方式/能力查询；若平台接口本身报错，可换相近能力获取数据。',
      }, ensure_ascii=False)

  # 创建同 schema 的新工具
  from langchain_core.tools import StructuredTool
  return StructuredTool.from_function(
    coroutine=safe_coroutine,
    name=tool.name,
    description=tool.description,
    args_schema=tool.args_schema,
  )
