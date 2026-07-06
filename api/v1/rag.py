"""
RAG 文件管理 API 端点
"""
import logging
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Query, File, UploadFile, Body, Request
from fastapi.responses import JSONResponse

from config.settings import settings
from config.mongodb_conn import mongodb_manager
from core.rbac import require_permission
from repository.vector_store import milvus_store as store
from repository.vector_store.milvus_store import _safe_group_dir
from service.rag.pipeline.rag_service import rag_service
from service.rag.utils.audit import log_upload, log_delete, log_query
from models.rag.schemas import UploadResponse, DeleteRequest, DeleteResponse
from config.trace_id import get_trace_id

logger = logging.getLogger(__name__)

router = APIRouter(tags=["rag"])


def _resolve_group_upload_path(group_id: str, filename: str) -> Path:
    safe_name = Path(filename).name
    return (Path(settings.UPLOAD_DIR) / _safe_group_dir(group_id) / safe_name).resolve()


def _resolve_group_embedded_dir(group_id: str) -> Path:
    return (Path(settings.EMBEDDED_DIR) / _safe_group_dir(group_id)).resolve()


@router.post("/upload")
@require_permission("ai:knowledge")
async def upload_file(
    request: Request,
    file: UploadFile = File(...),
    group_id: str = Query("default", description="知识库组 ID"),
):
    if not file.filename:
        return JSONResponse(status_code=400, content={"ok": False, "msg": "文件名为空"})

    group_id = group_id.strip() or "default"
    user_id = request.state.user_id
    safe_name = Path(file.filename).name
    suffix = Path(safe_name).suffix.lower().lstrip('.')
    supported = [e.strip() for e in settings.SUPPORTED_EXTENSIONS.split(",")]

    if suffix not in supported:
        return JSONResponse(
            status_code=400,
            content={
                "ok": False,
                "msg": f"不支持的文件类型 {suffix}，支持: {', '.join(sorted(supported))}",
            },
        )

    filepath = _resolve_group_upload_path(group_id, safe_name)
    upload_dir = filepath.parent
    logger.info("[Upload] 开始处理: filename=%s, group_id=%s, user_id=%s", safe_name, group_id, user_id)
    upload_dir.mkdir(parents=True, exist_ok=True)

    if filepath.exists():
        logger.info("[Upload] 检测到同名文件，覆盖更新: %s", filepath)
        try:
            vectors_deleted, file_deleted = rag_service.delete_file(str(filepath), group_id=group_id)
            logger.info("[Upload] 覆盖前清理: vectors=%d, file_deleted=%s", vectors_deleted, file_deleted)
        except Exception as e:
            logger.error("[Upload] 覆盖前清理失败: %s", e)
            return JSONResponse(
                status_code=500,
                content={"ok": False, "msg": "同名文件清理失败", "detail": str(e)},
            )

    try:
        content = await file.read()
        filepath.write_bytes(content)
        logger.info("[Upload] 文件保存成功: size=%d bytes", len(content))
    except Exception as e:
        logger.error("[Upload] 文件保存失败: %s", e)
        return JSONResponse(
            status_code=500,
            content={"ok": False, "msg": "文件保存失败", "detail": str(e)},
        )

    try:
        count = rag_service.add_file(str(filepath), group_id=group_id, open_id=user_id)
        logger.info("[Upload] 向量入库成功: group_id=%s, chunks=%d", group_id, count)

        # 写入 MongoDB 文件索引（加速后续查询，避免扫 Milvus）
        try:
          db = mongodb_manager.db
          await db.kb_files.update_one(
            {'group_id': group_id, 'source': str(filepath)},
            {'$set': {
              'group_id': group_id,
              'source': str(filepath),
              'file_name': safe_name,
              'size': filepath.stat().st_size,
              'chunks': count,
              'uploaded_by': user_id,
              'uploaded_at': datetime.utcnow(),
            }},
            upsert=True,
          )
        except Exception:
          logger.exception("[Upload] kb_files 写入失败（不影响主体流程）")

        try:
            embedded_dir = _resolve_group_embedded_dir(group_id)
            rag_service.export_chunks(str(filepath), output_dir=str(embedded_dir))
        except Exception as e:
            logger.error("[Upload] 切分导出失败: %s", e)

        log_upload(user_id, group_id, safe_name, count, get_trace_id())
        return UploadResponse(ok=True, name=safe_name, size=filepath.stat().st_size, chunks=count)
    except Exception as e:
        logger.error("[Upload] 向量入库失败: %s", e)
        return JSONResponse(
            status_code=500,
            content={"ok": False, "msg": "文件已保存但向量入库失败", "detail": str(e)},
        )


@router.post("/delete")
@require_permission("ai:knowledge")
async def delete_file(
    request: Request,
    body: DeleteRequest = Body(...),
    group_id: str = Query("default", description="知识库组 ID"),
):
    name = (body.name or "").strip()
    if not name:
        return JSONResponse(status_code=400, content={"ok": False, "msg": "文件名为空"})

    group_id = group_id.strip() or "default"
    user_id = request.state.user_id

    try:
        name_path = Path(name)
        if name_path.is_absolute() or "/" in name:
            filepath = name_path
        else:
            filepath = _resolve_group_upload_path(group_id, name)

        vectors_deleted, file_deleted = rag_service.delete_file(str(filepath), group_id=group_id)

        # 同步删除 MongoDB 文件索引
        try:
          await mongodb_manager.db.kb_files.delete_one({
            'group_id': group_id,
            'source': str(filepath),
          })
        except Exception:
          logger.exception("[Delete] kb_files 清理失败（不影响主体流程）")

        log_delete(user_id, group_id, Path(name).name, vectors_deleted, get_trace_id())

        return DeleteResponse(
            ok=True, name=name,
            vectors_deleted=vectors_deleted,
            file_deleted=file_deleted,
        )
    except Exception as e:
        logger.error("[Delete] 删除失败: %s", e)
        return JSONResponse(
            status_code=500,
            content={"ok": False, "msg": "删除失败", "detail": str(e)},
        )


@router.get("/vector-store-info")
@require_permission("ai:chat")
async def get_vector_store_info(
    request: Request,
    group_id: str = Query("default", description="知识库组 ID"),
    source: str = Query(None, description="文件来源过滤"),
):
    try:
        db = mongodb_manager.db
        gid = group_id.strip() or "default"
        query: dict = {"group_id": gid}
        if source:
            query["source"] = source
        records = await db.kb_files.find(query).to_list(None)

        sources: dict[str, dict] = {}
        total_chunks = 0
        for r in records:
            sources[r["source"]] = {"size": r.get("size", 0), "chunks": r.get("chunks", 0)}
            total_chunks += r.get("chunks", 0)
        return {"ok": True, "data": {"total_chunks": total_chunks, "sources": sources}}
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"ok": False, "msg": "获取向量库信息失败", "detail": str(e)},
        )


@router.post("/retrieve")
@require_permission("ai:chat")
async def retrieve_documents(
    request: Request,
    body: dict,
    group_id: str = Query("default", description="知识库组 ID"),
):
    try:
        query = body.get("query", "")
        k = body.get("k", settings.RAG_TOP_K)

        if not query:
            return JSONResponse(status_code=400, content={"ok": False, "msg": "查询内容为空"})

        group_id = group_id.strip() or "default"
        vector_store = store.get_vector_store(group_id=group_id)
        retriever = vector_store.as_retriever(search_kwargs={"k": k})
        docs = retriever.invoke(query.strip())

        documents = [
            {"content": doc.page_content, "metadata": doc.metadata}
            for doc in docs
        ]

        log_query(request.state.user_id, group_id, query.strip(), len(documents), get_trace_id())
        return {"ok": True, "documents": documents}
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"ok": False, "msg": "检索失败", "detail": str(e)},
        )


@router.get("/stats")
@require_permission("ai:chat")
async def get_overall_stats(request: Request):
    """获取所有知识库的文档总数和切片总数"""
    db = mongodb_manager.db
    try:
        pipeline = [
            {"$group": {"_id": None, "total_docs": {"$sum": 1}, "total_chunks": {"$sum": "$chunks"}}}
        ]
        result = await db.kb_files.aggregate(pipeline).to_list(1)
        if result:
            return {"ok": True, "total_docs": result[0]["total_docs"], "total_chunks": result[0]["total_chunks"]}
        return {"ok": True, "total_docs": 0, "total_chunks": 0}
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"ok": False, "msg": "获取统计信息失败", "detail": str(e)},
        )


# ==================== 知识库分组管理 ====================

@router.get("/groups")
@require_permission("ai:chat")
async def list_groups(request: Request):
    """列出所有知识库分组（全员可见）"""
    db = mongodb_manager.db
    docs = await db.knowledge_groups.find().sort("created_at", 1).to_list(None)
    groups = [
        {"group_id": d["group_id"], "name": d["name"], "created_at": d["created_at"]}
        for d in docs
    ]
    if not any(g["group_id"] == "default" for g in groups):
        groups.insert(0, {"group_id": "default", "name": "默认知识库", "created_at": None})
    return {"ok": True, "groups": groups}


@router.post("/groups")
@require_permission("ai:knowledge")
async def create_group(request: Request, body: dict):
    """创建知识库分组"""
    name = (body.get("name") or body.get("group_id") or "").strip()
    if not name:
        return JSONResponse(status_code=400, content={"ok": False, "msg": "名称不能为空"})
    group_id = name
    db = mongodb_manager.db
    now = datetime.utcnow().isoformat()
    try:
        await db.knowledge_groups.insert_one({
            "group_id": group_id,
            "name": name,
            "user_id": request.state.user_id,
            "created_at": now,
        })
    except Exception as e:
        if "duplicate key" in str(e).lower() or "E11000" in str(e):
            return JSONResponse(status_code=400, content={"ok": False, "msg": "分组已存在"})
        raise
    return {"ok": True, "group": {"group_id": group_id, "name": name, "created_at": now}}


@router.delete("/groups/{group_id}")
@require_permission("ai:knowledge")
async def delete_group(group_id: str, request: Request):
    """删除知识库分组"""
    if group_id == "default":
        return JSONResponse(status_code=400, content={"ok": False, "msg": "默认知识库不可删除"})
    db = mongodb_manager.db
    result = await db.knowledge_groups.delete_one({
        "user_id": request.state.user_id,
        "group_id": group_id,
    })
    if result.deleted_count == 0:
        return JSONResponse(status_code=404, content={"ok": False, "msg": "分组不存在"})
    return {"ok": True}
