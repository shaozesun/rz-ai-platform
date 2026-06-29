"""
RAG 相关数据模型
"""
from datetime import datetime
from typing import Optional
from pydantic import BaseModel, field_validator


class ChatRequest(BaseModel):
    text: str = ""
    created_at: Optional[datetime] = None

    @field_validator("created_at", mode="before")
    @classmethod
    def parse_created_at(cls, v):
        if v is None or v == "":
            return None
        return v


class UploadResponse(BaseModel):
    ok: bool
    name: str = ""
    size: int = 0
    chunks: int = 0
    msg: str = ""


class DeleteRequest(BaseModel):
    name: str = ""


class DeleteResponse(BaseModel):
    ok: bool
    name: str = ""
    vectors_deleted: int = 0
    file_deleted: bool = False
    msg: str = ""
