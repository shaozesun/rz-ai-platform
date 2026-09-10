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


class MessageListResponse(BaseModel):
    ok: bool = True
    messages: list[MessageListItem] = []
    total: int = 0


class MessageResponse(BaseModel):
    ok: bool = True
    message: Optional[Message] = None
    msg: str = ""
