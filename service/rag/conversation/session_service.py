"""
Session 管理服务（异步 Motor）
"""
from __future__ import annotations

import logging
import threading
import uuid
from datetime import datetime

from config.mongodb_conn import mongodb_manager
from models.rag.session_models import SessionBase, Session, SessionUpdate
from models.rag.message_models import MessageListItem
from service.rag.conversation.message_service import message_service

logger = logging.getLogger(__name__)


class SessionService:
    """Session 管理服务"""

    _instance: "SessionService | None" = None
    _lock = threading.Lock()
    _initialized: bool = False

    def __new__(cls) -> "SessionService":
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
    def sessions(self):
        return mongodb_manager.db.sessions

    async def create_session(self, session: SessionBase) -> Session | None:
        session_id = str(uuid.uuid4())

        from_datetime = (
            session.created_at
            if session.created_at is not None
            else datetime.now()
        )

        session_doc = {
            "session_id": session_id,
            "session_name": session.session_name,
            "user_id": session.user_id,
            "group_id": session.group_id,
            "created_at": from_datetime,
            "updated_at": datetime.now(),
            "last_activity_at": datetime.now(),
        }

        result = await self.sessions.insert_one(session_doc)
        inserted_doc = await self.sessions.find_one({"_id": result.inserted_id})
        if inserted_doc is None:
            return None
        return self._doc_to_session(inserted_doc)

    async def get_session(self, session_id: str, user_id: str | None = None) -> Session | None:
        query: dict = {"session_id": session_id}
        if user_id:
            query["user_id"] = user_id
        try:
            session_doc = await self.sessions.find_one(query)
        except Exception as e:
            logger.error(
                "[SessionService] 数据库查询失败: session_id=%s, error=%s",
                session_id,
                e,
            )
            return None

        if session_doc:
            try:
                return self._doc_to_session(session_doc)
            except Exception as e:
                logger.error(
                    "[SessionService] 转换失败: session_id=%s, error=%s",
                    session_id,
                    e,
                )
                return None

        return None

    async def get_user_sessions(
        self, user_id: str, skip: int = 0, limit: int = 100
    ) -> list[Session]:
        cursor = self.sessions.find(
            {"user_id": user_id}
        ).sort("created_at", -1).skip(skip).limit(limit)

        sessions = []
        async for doc in cursor:
            sessions.append(self._doc_to_session(doc))
        return sessions

    async def update_session(
        self, session_id: str, session: SessionUpdate, user_id: str | None = None
    ) -> Session | None:
        update_fields = {}
        if session.session_name is not None:
            update_fields["session_name"] = session.session_name
        if session.group_id is not None:
            update_fields["group_id"] = session.group_id
        update_fields["updated_at"] = datetime.now()

        query: dict = {"session_id": session_id}
        if user_id:
            query["user_id"] = user_id
        session_doc = await self.sessions.find_one(query)
        if not session_doc:
            return None

        await self.sessions.update_one(query, {"$set": update_fields})

        updated_doc = await self.sessions.find_one(query)
        if updated_doc is None:
            return None
        return self._doc_to_session(updated_doc)

    async def delete_session(self, session_id: str, user_id: str | None = None) -> bool:
        """先删 session，再清理 messages。即使消息清理失败，session 也已删除。"""
        query: dict = {"session_id": session_id}
        if user_id:
            query["user_id"] = user_id
        result = await self.sessions.delete_one(query)
        if result.deleted_count == 0:
            return False

        try:
            await message_service.delete_messages(session_id, user_id=user_id)
            logger.info(
                "[SessionService] 已删除 Session 的相关消息: session_id=%s",
                session_id,
            )
        except Exception as e:
            logger.error(
                "[SessionService] 删除消息失败（session 已删除）: session_id=%s, error=%s",
                session_id,
                e,
            )

        return True

    async def update_last_activity(self, session_id: str, user_id: str | None = None) -> bool:
        query: dict = {"session_id": session_id}
        if user_id:
            query["user_id"] = user_id
        result = await self.sessions.update_one(
            query,
            {"$set": {"last_activity_at": datetime.now()}}
        )
        return result.matched_count > 0

    async def get_group_by_session(self, session_id: str, user_id: str | None = None) -> str | None:
        query: dict = {"session_id": session_id}
        if user_id:
            query["user_id"] = user_id
        session_doc = await self.sessions.find_one(query)
        if session_doc:
            return session_doc.get("group_id")
        return None

    def _is_valid_uuid(self, session_id: str) -> bool:
        try:
            uuid_obj = uuid.UUID(session_id, version=4)
            return str(uuid_obj) == session_id
        except (ValueError, AttributeError):
            return False

    async def ensure_session_exists(self, session_id: str, user_id: str | None = None) -> str:
        if not self._is_valid_uuid(session_id):
            raise ValueError(f"无效的 session_id 格式: {session_id}")

        session = await self.get_session(session_id, user_id=user_id)
        if session is None:
            raise ValueError(f"Session 不存在: session_id={session_id}")

        return session_id

    async def get_session_messages(
        self, session_id: str, skip: int = 0, limit: int = 100,
        user_id: str | None = None,
    ) -> list[MessageListItem]:
        await self.ensure_session_exists(session_id, user_id=user_id)
        return await message_service.get_messages(session_id, skip, limit, user_id=user_id)

    def _doc_to_session(self, doc: dict) -> Session:
        return Session(
            id=str(doc.get("_id", "")),
            session_id=str(doc.get("session_id", "")),
            session_name=str(doc.get("session_name", "")),
            user_id=str(doc.get("user_id", "")),
            group_id=str(doc.get("group_id", "")),
            created_at=doc.get("created_at"),
            updated_at=doc.get("updated_at") or datetime.now(),
        )


session_service = SessionService()
