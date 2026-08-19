"""RZ Agent 状态定义（参照 DeerFlow ThreadState 多 channel 设计）"""

from typing import Annotated
from langgraph.graph.message import add_messages
from langgraph.graph import MessagesState


class RzAgentState(MessagesState):
  """RZ Agent 共享状态。

  参照 DeerFlow ThreadState 的多 channel 模式（messages / summary_text / artifacts），
  在 MessagesState 基础上增加 summary 和 promoted 两个 channel。
  """
  messages: Annotated[list, add_messages]
  # 上下文摘要（旧消息压缩结果），参照 DeerFlow ThreadState.summary_text
  summary: str | None
  # deferred 工具 promote 状态（catalog_hash + promoted tool names）
  promoted: dict | None
