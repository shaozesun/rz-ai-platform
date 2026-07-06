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


class MessageCreate(MessageBase):
    pass


class Message(MessageBase):
    id: str


class MessageListItem(BaseModel):
    role: str = Field(..., description="消息角色 (user/assistant)")
    content: str = Field(..., description="消息内容")
    created_at: Optional[datetime] = None


class MessageListResponse(BaseModel):
    ok: bool = True
    messages: list[MessageListItem] = []
    total: int = 0


class MessageResponse(BaseModel):
    ok: bool = True
    message: Optional[Message] = None
    msg: str = ""
