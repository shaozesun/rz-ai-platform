"""
消息数据模型
"""
from datetime import datetime
from typing import Optional
from pydantic import BaseModel, Field


class MessageBase(BaseModel):
    session_id: str = Field(..., description="Session ID")
    message_id: str = Field(..., description="消息 ID")
    role: str = Field(..., description="消息角色 (user/assistant)")
    content: str = Field(..., description="消息内容")
    user_id: Optional[str] = Field(None, description="用户 ID")
    created_at: datetime = Field(..., description="消息创建时间")
    uploaded_filenames: Optional[list[str]] = Field(
        None, description="user 消息关联的已上传文件名（结构化字段，不进 content 纯文本）",
    )
    context_tokens: Optional[int] = Field(
        None, description="assistant 消息：本轮模型上报的输入 token 数（= 当时上下文大小，供前端显示用量）",
    )
    todos: Optional[list] = Field(
        None, description="assistant 消息：计划模式 write_todos 最后一次提交的任务清单（供前端还原托盘）",
    )
    chart_data: Optional[dict] = Field(
        None,
        description='assistant 消息：本轮图表的**数据**（{组名: rows}）。模型只在正文写 '
                    '`<chart group="组名"/>`，数据由服务端从 analysis_exec 的返回值注入 —— '
                    '正文里不再出现 JSON（写正文会被引号截断、图直接崩）。刷新/历史重放时按组名取用。',
    )


class MessageCreate(MessageBase):
    pass


class Message(MessageBase):
    id: str


class MessageListItem(BaseModel):
    message_id: str = Field('', description="消息 ID（按条删除用）")
    role: str = Field(..., description="消息角色 (user/assistant)")
    content: str = Field(..., description="消息内容")
    created_at: Optional[datetime] = None
    uploaded_filenames: Optional[list[str]] = Field(
        None, description="user 消息关联的已上传文件名",
    )
    context_tokens: Optional[int] = Field(
        None, description="assistant 消息：本轮上下文 token 数（前端显示「上下文 X%」）",
    )
    todos: Optional[list] = Field(
        None, description="assistant 消息：计划模式任务清单（前端还原输入框上方的进度托盘）",
    )
    chart_data: Optional[dict] = Field(
        None, description='assistant 消息：图表数据（{组名: rows}），前端按 `<chart group="组名"/>` 取用',
    )


class AppendUserMessageRequest(BaseModel):
    """把用户消息先落库（开流前调用，见 api/v1/chat.py 的同名端点）。"""
    content: str = Field(..., description="用户消息正文")
    uploaded_filenames: Optional[list[str]] = Field(
        None, description="本轮关联的已上传文件名（结构化字段，不进 content）",
    )


class MessageListResponse(BaseModel):
    ok: bool = True
    messages: list[MessageListItem] = []
    total: int = 0
    context_window: Optional[int] = Field(
        None, description="模型上下文窗口 token 数（前端算「已用 %」的分母）",
    )
    compact_trigger_tokens: Optional[int] = Field(
        None, description="自动压缩触发的 token 阈值（前端提示「压缩阈值 32k」）",
    )


class MessageResponse(BaseModel):
    ok: bool = True
    message: Optional[Message] = None
    msg: str = ""
