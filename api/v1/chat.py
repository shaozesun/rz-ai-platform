"""
RAG 对话 API 端点（流式 SSE）
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse

from core.rbac import require_permission
from core.scheduler.rag_llm_patch import set_chat_role
from core.scheduler.priority import PriorityCalculator
from config.trace_id import get_trace_id, set_trace_id
from service.rag.pipeline.rag_service import rag_service
from service.rag.utils.llm_output_filter import ThinkingStreamFilter, strip_thinking_text
from service.rag.conversation.chat_history import _current_user_id
from service.rag.conversation.message_service import message_service
from service.rag.conversation.session_service import session_service
from models.rag.message_models import MessageCreate, MessageListResponse
from models.rag.schemas import ChatRequest
from models.rag.session_models import (
  SessionCreate,
  SessionListResponse,
  SessionResponse,
  DeleteSessionResponse,
)

logger = logging.getLogger(__name__)


def _chunk_to_text(chunk) -> str:
    content = chunk.content if hasattr(chunk, "content") else chunk
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                parts.append(str(item.get("text", "")))
            elif hasattr(item, "text"):
                parts.append(str(item.text))
        return "".join(parts)
    return str(content) if content is not None else ""


async def generate_rag_stream(
    text: str,
    session_id: str,
    group_id: str,
    selected_source: str | None = None,
    trace_id: str = "",
    user_id: str | None = None,
):
    if trace_id:
        set_trace_id(trace_id)

    token = _current_user_id.set(user_id)
    try:
        chain = rag_service.get_chain(group_id=group_id, selected_source=selected_source)

        question = text.strip()
        stream_filter = ThinkingStreamFilter()
        answer_parts: list[str] = []

        try:
            async for chunk in chain.astream(
                {"question": question, "session_id": session_id},
                config={"configurable": {"session_id": session_id}},
            ):
                raw = _chunk_to_text(chunk)
                if not raw:
                    continue
                cleaned = stream_filter.feed(raw)
                if not cleaned:
                    continue
                answer_parts.append(cleaned)
                yield f"data: {json.dumps({'content': cleaned}, ensure_ascii=False)}\n\n"

            tail = stream_filter.flush()
            if tail:
                answer_parts.append(tail)
                yield f"data: {json.dumps({'content': tail}, ensure_ascii=False)}\n\n"

            yield "data: [DONE]\n\n"

            final_answer = strip_thinking_text("".join(answer_parts))
            logger.info("[Chat][RAG] 最终回答 q=%s answer_len=%d", question[:100], len(final_answer))
        except Exception as e:
            logger.error("[RAG 流式] 生成失败: %s", e)
            yield f"data: {json.dumps({'content': f'[错误] 生成回复失败: {str(e)}'}, ensure_ascii=False)}\n\n"
        finally:
            ai_content = strip_thinking_text("".join(answer_parts)).strip()
            if ai_content:
                try:
                    all_msgs = await message_service.get_messages(session_id, user_id=user_id)
                    last_msg = all_msgs[-1] if all_msgs else None
                    already_saved = (
                        last_msg is not None
                        and last_msg.role == "assistant"
                        and last_msg.content == ai_content
                    )
                    if not already_saved:
                        await message_service.create_message(MessageCreate(
                            session_id=session_id,
                            role="assistant",
                            content=ai_content,
                            user_id=user_id,
                            created_at=datetime.now(),
                        ))
                except Exception:
                    logger.exception("[Chat] 保存AI回复失败")
    finally:
        _current_user_id.reset(token)


async def generate_no_rag_stream(text: str, session_id: str, trace_id: str = "",
                                user_id: str | None = None):
    if trace_id:
        set_trace_id(trace_id)

    token = _current_user_id.set(user_id)
    try:
        queue = asyncio.Queue()

        async def send(chunk):
            await queue.put(chunk)

        question = text.strip()
        stream_filter = ThinkingStreamFilter()
        answer_parts: list[str] = []

        async def stream_llm(send_func):
            try:
                async for chunk in rag_service.async_answer_without_rag(question, session_id=session_id):
                    cleaned = stream_filter.feed(chunk)
                    if not cleaned:
                        continue
                    answer_parts.append(cleaned)
                    await send_func(f"data: {json.dumps({'content': cleaned}, ensure_ascii=False)}\n\n")

                tail = stream_filter.flush()
                if tail:
                    answer_parts.append(tail)
                    await send_func(f"data: {json.dumps({'content': tail}, ensure_ascii=False)}\n\n")
            except Exception as e:
                logger.error("[普通流式] 生成失败: %s", e)
                await send_func(f"data: {json.dumps({'content': f'[错误] 生成回复失败: {str(e)}'}, ensure_ascii=False)}\n\n")
            finally:
                await send_func(None)

        task = asyncio.create_task(stream_llm(send))

        try:
            while True:
                tok = await queue.get()
                if tok is None:
                    yield "data: [DONE]\n\n"
                    break
                yield tok
        finally:
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            ai_content = strip_thinking_text("".join(answer_parts)).strip()
            if ai_content:
                try:
                    all_msgs = await message_service.get_messages(session_id, user_id=user_id)
                    last_msg = all_msgs[-1] if all_msgs else None
                    already_saved = (
                        last_msg is not None
                        and last_msg.role == "assistant"
                        and last_msg.content == ai_content
                    )
                    if not already_saved:
                        await message_service.create_message(MessageCreate(
                            session_id=session_id,
                            role="assistant",
                            content=ai_content,
                            user_id=user_id,
                            created_at=datetime.now(),
                        ))
                except Exception:
                    logger.exception("[Chat] 保存AI回复失败")
    finally:
        _current_user_id.reset(token)


router = APIRouter(prefix="/chat", tags=["chat"])


@router.post("")
@require_permission("ai:chat")
async def chat(
    request: Request,
    body: ChatRequest,
    session_id: str = Query(..., description="会话 ID"),
    group_id: str = Query("default", description="知识库组 ID"),
    selected_source: str = Query(None, description="指定文档源路径"),
):
    text = (body.text or "").strip()
    if not text:
        return JSONResponse(status_code=400, content={"ok": False, "msg": "内容为空"})

    user_id = request.state.user_id
    group_id = group_id.strip() or "default"

    # 校验会话所有权：不存在则自动创建，属于他人则拒绝
    session = await session_service.get_session(session_id)
    if session is None:
        from models.rag.session_models import SessionBase
        session = await session_service.create_session(SessionBase(
            session_name=text[:30] or "新对话",
            user_id=user_id,
            group_id=group_id,
        ))
        if not session:
            return JSONResponse(status_code=500, content={"ok": False, "msg": "创建会话失败"})
        session_id = session.session_id
    elif session.user_id != user_id:
        return JSONResponse(status_code=403, content={"ok": False, "msg": "无权访问该会话"})

    role = PriorityCalculator.resolve_role(request)
    set_chat_role(role)

    return StreamingResponse(
        generate_rag_stream(
            text,
            session_id=session_id,
            group_id=group_id,
            selected_source=selected_source,
            trace_id=get_trace_id(),
            user_id=user_id,
        ),
        media_type="text/event-stream; charset=utf-8",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/no-rag")
@require_permission("ai:chat")
async def chat_no_rag(
    request: Request,
    body: ChatRequest,
    session_id: str = Query(..., description="会话 ID"),
):
    text = (body.text or "").strip()
    if not text:
        return JSONResponse(status_code=400, content={"ok": False, "msg": "内容为空"})

    user_id = request.state.user_id

    # 校验会话所有权
    session = await session_service.get_session(session_id)
    if session is None:
        from models.rag.session_models import SessionBase
        session = await session_service.create_session(SessionBase(
            session_name=text[:30] or "新对话",
            user_id=user_id,
            group_id="default",
        ))
        if not session:
            return JSONResponse(status_code=500, content={"ok": False, "msg": "创建会话失败"})
        session_id = session.session_id
    elif session.user_id != user_id:
        return JSONResponse(status_code=403, content={"ok": False, "msg": "无权访问该会话"})

    role = PriorityCalculator.resolve_role(request)
    set_chat_role(role)

    return StreamingResponse(
        generate_no_rag_stream(text, session_id=session_id, trace_id=get_trace_id(),
                               user_id=user_id),
        media_type="text/event-stream; charset=utf-8",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ==================== Session 管理 ====================

@router.get("/sessions")
@require_permission("ai:chat")
async def list_sessions(request: Request):
    sessions = await session_service.get_user_sessions(request.state.user_id)
    return SessionListResponse(
        sessions=sessions,
        total=len(sessions),
    )


@router.post("/sessions")
@require_permission("ai:chat")
async def create_session_endpoint(
    request: Request,
    group_id: str = Query("default", description="知识库组 ID"),
    title: str = Query("", description="会话标题"),
):
    from models.rag.session_models import SessionBase

    session = await session_service.create_session(SessionBase(
        session_name=title or "新对话",
        user_id=request.state.user_id,
        group_id=group_id,
    ))
    if not session:
        return JSONResponse(status_code=500, content={"ok": False, "msg": "创建会话失败"})
    return SessionResponse(ok=True, session=session)


@router.delete("/sessions/{session_id}")
@require_permission("ai:chat")
async def delete_session_endpoint(
    request: Request,
    session_id: str,
):
    ok = await session_service.delete_session(session_id, user_id=request.state.user_id)
    if not ok:
        return JSONResponse(status_code=404, content={"ok": False, "msg": "会话不存在"})
    return DeleteSessionResponse(ok=True, msg="已删除")


@router.patch("/sessions/{session_id}")
@require_permission("ai:chat")
async def update_session_endpoint(
    request: Request,
    session_id: str,
    body: dict,
):
    from models.rag.session_models import SessionUpdate
    title = (body.get("title") or body.get("session_name") or "").strip()
    if not title:
        return JSONResponse(status_code=400, content={"ok": False, "msg": "标题不能为空"})
    updated = await session_service.update_session(
        session_id, SessionUpdate(session_name=title), user_id=request.state.user_id)
    if not updated:
        return JSONResponse(status_code=404, content={"ok": False, "msg": "会话不存在"})
    return {"ok": True, "session": updated.model_dump(mode="json")}


@router.get("/sessions/{session_id}/messages")
@require_permission("ai:chat")
async def get_session_messages(
    request: Request,
    session_id: str,
):
    try:
        messages = await session_service.get_session_messages(
            session_id, user_id=request.state.user_id)
        return MessageListResponse(
            messages=messages,
            total=len(messages),
        )
    except ValueError as e:
        return JSONResponse(status_code=400, content={"ok": False, "msg": str(e)})
