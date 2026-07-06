"""
LangChain 兼容的聊天历史记录接口（MongoDB 版本）

注意：此模块必须保持同步接口，因为 LangChain 的 BaseChatMessageHistory
要求 add_message / messages 为 sync 方法。所有 DB 操作使用
mongodb_manager.sync_db（PyMongo），不依赖异步 message_service。

消息操作极快（<1ms 索引查询），对事件循环的阻塞可忽略。
"""
import contextvars
import logging
import uuid
from datetime import datetime
from typing import cast

from langchain_core.chat_history import BaseChatMessageHistory
from langchain_core.messages import BaseMessage, HumanMessage, AIMessage

from config.mongodb_conn import mongodb_manager

logger = logging.getLogger(__name__)

# 当前请求用户 ID，由 chat API 在调用链前设置，解决 LangChain
# RunnableWithMessageHistory 无法透传 user_id 的问题
_current_user_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    'current_user_id', default=None
)


class MongoDBChatMessageHistory(BaseChatMessageHistory):
    """基于 MongoDB 的聊天消息历史"""

    def __init__(self, session_id: str, max_messages: int = 6,
                 user_id: str | None = None) -> None:
        self.session_id = session_id
        self.user_id = user_id
        self._max_messages = max_messages
        self._messages: list[BaseMessage] = []
        self._load_messages()

    @property
    def _collection(self):
        return mongodb_manager.sync_db["messages"]

    def _build_query(self) -> dict:
        query: dict = {"session_id": self.session_id}
        if self.user_id:
            query["$or"] = [
                {"user_id": self.user_id},
                {"user_id": {"$exists": False}},
            ]
        return query

    def _load_messages(self) -> None:
        try:
            collection = self._collection
            query = self._build_query()
            total = collection.count_documents(query)
            skip = max(0, total - self._max_messages)
            messages = collection.find(query
            ).sort("created_at", 1).skip(skip)

            self._messages: list[BaseMessage] = [
                cast(BaseMessage,
                    HumanMessage(content=cast(str, msg.get("content")))
                    if cast(str, msg.get("role")) == "user"
                    else AIMessage(content=cast(str, msg.get("content")))
                )
                for msg in messages
            ]
        except Exception as e:
            logger.error(
                "[MongoDBChatMessageHistory] 加载消息失败: session_id=%s, error=%s",
                self.session_id,
                e,
            )
            self._messages = []

    @property
    def messages(self) -> list[BaseMessage]:
        return self._messages[-self._max_messages:]

    def add_message(self, message: BaseMessage) -> None:
        try:
            role = "user" if isinstance(message, HumanMessage) else "assistant"
            content_str = (
                cast(str, message.content)
                if isinstance(message.content, str)
                else str(message.content)
            )

            doc: dict = {
                "message_id": uuid.uuid4().hex,
                "session_id": self.session_id,
                "role": role,
                "content": content_str,
                "created_at": datetime.now(),
            }
            if self.user_id:
                doc["user_id"] = self.user_id
            self._collection.insert_one(doc)
            self._messages.append(message)
        except Exception as e:
            logger.error(
                "[MongoDBChatMessageHistory] 保存消息失败: session_id=%s, error=%s",
                self.session_id,
                e,
            )

    def clear(self) -> None:
        self._messages = []


def get_session_history(
    session_id: str, max_messages: int = 6, user_id: str | None = None,
) -> MongoDBChatMessageHistory:
    effective_user_id = user_id or _current_user_id.get()
    return MongoDBChatMessageHistory(
        session_id, max_messages=max_messages, user_id=effective_user_id,
    )
