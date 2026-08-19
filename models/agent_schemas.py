"""Agent 相关数据模型"""

from typing import Optional
from pydantic import BaseModel, Field


class AgentRequest(BaseModel):
  """Agent 对话请求"""
  message: str = Field(..., description='用户消息')
  session_id: Optional[str] = Field(None, description='会话 ID，不传则自动创建')
  group_id: str = Field('default', description='知识库组 ID')
  interaction_mode: str = Field('trust', description='交互模式：trust 自主执行 / plan 先列计划确认后执行')
  plan_confirmed: bool = Field(False, description='计划模式下是否已确认执行')
  plan: Optional[dict] = Field(None, description='已确认的执行计划（前端编辑后回传）')
  interview_answers: Optional[dict] = Field(
    None, description='访谈轮用户对各问题的回答（{问题 id: 选择值}），携带则进入计划生成轮',
  )
  interview_action: Optional[str] = Field(
    None, description='访谈轮固定动作：answer 提交回答 / skip 跳过提问直接计划 / chat 聊点别的（退出计划走对话）',
  )


class AgentEvent(BaseModel):
  """Agent SSE 事件（发送给前端）"""
  event: str  # text / tool_call / tool_result / error / done
  data: dict
