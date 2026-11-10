"""Agent 相关数据模型"""

from typing import Optional
from pydantic import BaseModel, Field


class AgentRequest(BaseModel):
  """Agent 对话请求"""
  message: str = Field(..., description='用户消息')
  session_id: Optional[str] = Field(None, description='会话 ID，不传则自动创建')
  group_id: str = Field('default', description='知识库组 ID')
  interaction_mode: str = Field(
    'trust',
    description='交互模式：trust 自主执行 / plan 计划模式（执行中用 write_todos 维护任务清单、前端实时显示进度）',
  )
  task_mode: str = Field(
    'normal',
    description='任务模式：normal 常规 / full_analysis 全量分析（优先多维度数据分析）/ research 联网调研',
  )
  web_search: bool = Field(
    False, description='本轮是否放开联网检索（web__search / web__fetch），仅放开工具、不注入调研规范',
  )
  uploaded_filenames: Optional[list[str]] = Field(
    None, description='本轮消息关联的已上传文件名（上传接口返回的 filename），触发 <current_uploads> 自动注入',
  )


class AgentEvent(BaseModel):
  """Agent SSE 事件（发送给前端）"""
  event: str  # text / tool_call / tool_result / error / done
  data: dict
