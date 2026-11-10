"""
Session 数据模型
"""
from datetime import datetime
from typing import Optional
from pydantic import BaseModel, Field


class SessionBase(BaseModel):
    session_name: str = Field(..., description="Session 名称")
    user_id: str = Field(..., description="用户 ID")
    group_id: str = Field(..., description="所属组 ID")
    created_at: Optional[datetime] = Field(None, description="创建时间")


class SessionCreate(SessionBase):
    pass


class SessionUpdate(BaseModel):
    session_name: Optional[str] = Field(None)
    group_id: Optional[str] = Field(None)
    pinned: Optional[bool] = Field(None, description="置顶")


class Session(SessionBase):
    id: str
    session_id: str
    updated_at: datetime
    pinned: bool = False
    message_count: int = Field(
        0,
        description="该会话的消息条数（get_user_sessions 批量聚合填充）。前端据此判断"
                    "会话是不是空壳——**不能用标题判断**（标题可能长期停在占位名「新对话」，"
                    "按标题过滤会把有内容的会话一起藏掉）",
    )


class SessionListResponse(BaseModel):
    ok: bool = True
    sessions: list[Session] = []
    total: int = 0


class SessionResponse(BaseModel):
    ok: bool = True
    session: Optional[Session] = None
    msg: str = ""


class DeleteSessionResponse(BaseModel):
    ok: bool = True
    msg: str = ""
