"""
消息管理服务（异步 Motor）
"""
from __future__ import annotations

import logging
import threading
import uuid
from datetime import datetime
from typing import Optional, cast

from config.mongodb_conn import mongodb_manager
from models.rag.message_models import MessageCreate, Message, MessageListItem

logger = logging.getLogger(__name__)


class MessageService:
    """Message 消息管理服务"""

    _instance: "MessageService | None" = None
    _lock = threading.Lock()
    _initialized: bool = False

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    instance = super().__new__(cls)
                    instance._initialized = False
                    cls._instance = instance

        if not cls._instance._initialized:
            cls._instance._initialized = True

        return cls._instance

    def __init__(self):
        if getattr(self, "_initialized", False):
            return

    @property
    def messages(self):
        return mongodb_manager.db.messages

    async def create_message(self, message: MessageCreate) -> Message | None:
        message_id = str(uuid.uuid4())

        message_doc = {
            "message_id": message_id,
            "session_id": message.session_id,
            "role": message.role,
            "content": message.content,
            "created_at": message.created_at,
        }

        result = await self.messages.insert_one(message_doc)

        inserted_doc = await self.messages.find_one({"_id": result.inserted_id})
        if inserted_doc is None:
            logger.error("[MessageService] 插入消息后无法找到文档: message_id=%s", message_id)
            return None
        return self._doc_to_message(inserted_doc)

    async def get_messages(
        self, session_id: str, skip: int = 0, limit: int = 100
    ) -> list[MessageListItem]:
        try:
            cursor = self.messages.find(
                {"session_id": session_id}
            ).sort("created_at", 1).skip(skip).limit(limit)

            messages = []
            async for msg in cursor:
                messages.append(MessageListItem(
                    role=cast(str, msg.get("role")),
                    content=cast(str, msg.get("content")),
                    created_at=cast(Optional[datetime], msg.get("created_at")),
                ))
            return messages
        except Exception as e:
            logger.error(
                "[MessageService] 查询消息失败: session_id=%s, error=%s",
                session_id,
                e,
            )
            raise

    async def delete_messages(self, session_id: str) -> int:
        result = await self.messages.delete_many({"session_id": session_id})
        return result.deleted_count

    def _doc_to_message(self, doc: dict) -> Message:
        created_at = doc.get("created_at")
        if created_at is None:
            created_at = datetime.now()
        return Message(
            id=str(doc.get("_id", "")),
            message_id=cast(str, doc.get("message_id")),
            session_id=cast(str, doc.get("session_id")),
            role=cast(str, doc.get("role")),
            content=cast(str, doc.get("content")),
            created_at=cast(datetime, created_at),
        )


message_service = MessageService()
