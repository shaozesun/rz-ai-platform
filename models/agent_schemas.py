"""Agent 相关数据模型"""

from typing import Optional
from pydantic import BaseModel, Field


class AgentRequest(BaseModel):
  """Agent 对话请求"""
  message: str = Field(..., description='用户消息')
  session_id: Optional[str] = Field(None, description='会话 ID，不传则自动创建')
  group_id: str = Field('default', description='知识库组 ID')


class AgentEvent(BaseModel):
  """Agent SSE 事件（发送给前端）"""
  event: str  # text / tool_call / tool_result / error / done
  data: dict
