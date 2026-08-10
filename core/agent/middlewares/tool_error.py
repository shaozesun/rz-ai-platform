"""工具异常处理 — 捕获工具异常返回友好消息，Agent 不崩溃"""

import json
import functools
import logging

logger = logging.getLogger(__name__)


def wrap_tool_with_error_handling(tool):
  """包装工具：异常时返回友好错误消息而非崩溃"""
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
