"""RZ Agent 状态定义"""

from typing import Annotated
from langgraph.graph.message import add_messages
from langgraph.graph import MessagesState


class RzAgentState(MessagesState):
  """RZ Agent 共享状态，继承 MessagesState 的 messages 字段"""
  messages: Annotated[list, add_messages]
