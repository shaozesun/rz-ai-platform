"""
向量存储：使用 Milvus 向量库（服务器模式），支持多权限组
使用 pymilvus.MilvusClient 类进行实现
"""
from __future__ import annotations

import re
import logging
import shutil
import traceback
import uuid
from pathlib import Path
from typing import Any
from langchain_core.documents import Document

from config.settings import settings

from repository.ingestion import loader, splitter
import urllib.parse
from langchain_openai import OpenAIEmbeddings
from pydantic import SecretStr
from pymilvus import MilvusClient, DataType

logger = logging.getLogger(__name__)


def _milvus_hit_summary(hit: Any) -> str:
    """检索 hit 摘要（不含 embedding/text）。"""
    if hit is None:
        return "no hit"
    getter = hit.get if hasattr(hit, "get") else lambda k, default=None: getattr(hit, k, default)
    source = str(getter("source", "") or "")
    file_name = str(getter("file_name", "") or "").strip() or (
        Path(source).name if source else "-"
    )
    return (
        f"id={getter('id', '-')!s} dist={float(getter('distance', 0) or 0):.4f} "
        f"file={file_name} chunk={getter('chunk_index', '-')!s}"
    )


def _safe_group_dir(group_id: str) -> str:
    """与 api.v1.endpoints.rag._safe_group_dir 保持一致：组目录名。"""
    normalized = (group_id or "").strip()
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", normalized).strip("._")
    return safe or "group_default"


_project_root = Path(__file__).resolve().parent.parent
EXPORT_CHUNKS_DIR = str(_project_root / "embedded")

COLLECTION_NAME = "rag_docs"


class FileMetadataExtractor:
    """文件元数据提取器，用于从文件名中提取元数据信息"""

    @staticmethod
    def normalize_path(path: str) -> str:
        if not path:
            return path
        decoded = urllib.parse.unquote(path)
        normalized = decoded.replace("\\", "/")
        return normalized


class MilvusVectorStore:
    """Milvus 向量存储封装类（使用 pymilvus.MilvusClient）"""

    def __init__(self, collection_name: str, embedding_function, sparse_embedder=None):
        self.collection_name = collection_name
        self.embedding_function = embedding_function
        self._sparse_embedder = sparse_embedder
        self.client = self._get_milvus_client()
        self._embedding_dim = None
        self._collection_loaded = False

        try:
            logger.debug(
                f"[Milvus] 开始初始化 Milvus 向量存储，collection_name={collection_name}"
            )

            if self.client.has_collection(collection_name=self.collection_name):
                logger.debug(
                    f"[Milvus] 集合已存在: {self.collection_name}，将使用现有集合"
                )
            else:
                logger.debug(f"[Milvus] 集合不存在，开始创建: {self.collection_name}")
                try:
                    self._create_collection()
                except Exception as create_err:
                    err_msg = str(create_err)
                    # create_collection 失败且含 "not loaded" → Milvus 内部残留旧 collection 状态，
                    # drop 清理后重试一次
                    if 'not loaded' in err_msg:
                        logger.warning(
                            "[Milvus] create_collection 失败（%s），检测到 Milvus 内部残留状态，"
                            "执行 drop_collection 清理后重试。注意：此操作会清空 rag_docs 中的所有向量数据。",
                            err_msg,
                        )
                        try:
                            self.client.drop_collection(collection_name=self.collection_name)
                        except Exception as drop_err:
                            logger.warning("[Milvus] drop_collection 清理失败: %s", drop_err)
                        # 重试创建
                        self._create_collection()
                    else:
                        raise
                logger.debug(f"[Milvus] 集合创建成功: {self.collection_name}")

            logger.debug("[Milvus] Milvus 向量存储初始化成功")
        except Exception as e:
            logger.exception("[Milvus] 初始化 Milvus 向量存储失败")
            raise

    def _get_milvus_client(self):
        """获取 Milvus 客户端实例（单例）"""
        if not hasattr(self, '_milvus_client') or self._milvus_client is None:
            self._milvus_client = MilvusClient(uri=settings.MILVUS_URI, token=settings.MILVUS_TOKEN, db_name=settings.MILVUS_DB_NAME, timeout=30)
            logger.debug("[Milvus] Milvus 客户端创建成功")
        return self._milvus_client

    def _ensure_loaded(self):
        """确保 collection 已加载：检测状态，空跳过，未加载则加载"""
        if self._collection_loaded:
            return
        try:
            # 先检查数据量，空 collection 直接跳过（加载空 collection 会卡死）
            row_count = 0
            try:
                stats = self.client.get_collection_stats(collection_name=self.collection_name)
                row_count = stats.get('row_count', 0) if isinstance(stats, dict) else 0
            except Exception:
                pass
            if row_count == 0:
                logger.info('[Milvus] collection 为空，跳过加载: %s', self.collection_name)
                self._collection_loaded = True
                return

            # 检查是否已加载
            try:
                load_info = self.client.get_load_state(collection_name=self.collection_name)
                state_str = str(load_info.get('state', '')) if isinstance(load_info, dict) else str(load_info)
                if 'Loaded' in state_str:
                    self._collection_loaded = True
                    return
            except Exception:
                pass

            logger.info('[Milvus] collection 未加载，正在加载 (rows=%s)...', row_count)
            self.client.load_collection(collection_name=self.collection_name)
            self._collection_loaded = True
            logger.info('[Milvus] collection 加载完成: %s', self.collection_name)
        except Exception:
            logger.exception('[Milvus] 加载 collection 失败')

    def _get_embedding_dim(self) -> int:
        """获取嵌入向量的维度"""
        if self._embedding_dim is None:
            test_text = ["test"]
            try:
                embeddings = self.embedding_function.embed_documents(test_text)
                self._embedding_dim = len(embeddings[0])
                logger.debug(f"[Milvus] 嵌入维度检测完成: {self._embedding_dim}")
            except Exception as e:
                logger.error(f"[Milvus] 自动检测嵌入维度失败: {e}")
                raise RuntimeError(f"无法检测 Embedding 维度，请确认 Embedding 服务是否正常: {e}") from e
        return self._embedding_dim

    def _create_collection(self):
        """创建 Milvus 集合"""
        try:
            fields = [
                MilvusClient.create_schema(
                    auto_id=False,
                    enable_dynamic_field=True,
                )
            ]

            fields[0].add_field(
                field_name="id",
                datatype=DataType.VARCHAR,
                is_primary=True,
                max_length=64,
            )
            fields[0].add_field(
                field_name="text",
                datatype=DataType.VARCHAR,
                max_length=65535,
            )
            fields[0].add_field(
                field_name="embedding",
                datatype=DataType.FLOAT_VECTOR,
                dim=self._get_embedding_dim(),
            )
            fields[0].add_field(
                field_name="source",
                datatype=DataType.VARCHAR,
                max_length=512,
            )
            fields[0].add_field(
                field_name="chunk_index",
                datatype=DataType.INT64,
            )
            fields[0].add_field(
                field_name="sparse_embedding",
                datatype=DataType.SPARSE_FLOAT_VECTOR,
            )

            index_params = self.client.prepare_index_params()
            index_params.add_index(
                field_name="embedding",
                index_type="AUTOINDEX",
                metric_type="COSINE",
            )
            index_params.add_index(
                field_name="sparse_embedding",
                index_type="SPARSE_INVERTED_INDEX",
                metric_type="IP",
            )

            try:
                self.client.create_collection(
                    collection_name=self.collection_name,
                    schema=fields[0],
                    index_params=index_params,
                    timeout=30,
                )
            except Exception as e:
                err_msg = str(e)
                # pymilvus 3.x 的 create_collection 内部会自动 load_collection，
                # 当 Milvus 残留了同名旧 collection 数据时 auto-load 会报 "collection not loaded"。
                # 此时 collection schema 通常已创建成功，仅 auto-load 失败（空 collection 无需加载）。
                if 'not loaded' in err_msg and self.client.has_collection(
                    collection_name=self.collection_name
                ):
                    logger.warning(
                        "[Milvus] create_collection 的 auto-load 失败但 schema 已创建，"
                        "跳过自动加载（由 _ensure_loaded 按需加载）: %s, err=%s",
                        self.collection_name, err_msg,
                    )
                else:
                    logger.error(f"[Milvus] 创建集合失败: {str(e)}")
                    raise

            logger.debug(f"[Milvus] 集合创建成功: {self.collection_name}")
        except Exception as e:
            logger.error(f"[Milvus] 创建集合失败: {str(e)}")
            raise

    @staticmethod
    def _extract_dynamic_metadata(item: dict[str, Any]) -> dict[str, Any]:
        """仅提取约定的动态字段，避免 $meta 下出现无关内容。"""
        dynamic_keys = (
            "file_name",
            "sheet",
            "start_index",
            "parent_id",
            "parent_chunk_index",
            "parent_global_index",
            "child_chunk_index",
            "parent_text",
            "doc_type",
            "subsystem",
            "section_type",
            "group_id",
        )
        return {key: item.get(key) for key in dynamic_keys if item.get(key) is not None}

    def add_documents(self, documents: list[Document]) -> list[str]:
        """添加文档到 Milvus"""
        if not documents:
            return []

        try:
            logger.debug(f"[Milvus] 开始添加文档，数量: {len(documents)}")

            data = []
            ids = []
            for i, doc in enumerate(documents):
                doc_id = str(uuid.uuid4())
                ids.append(doc_id)
                dynamic_metadata = self._extract_dynamic_metadata(doc.metadata)
                source_value = FileMetadataExtractor.normalize_path(doc.metadata.get("source", ""))
                entry = {
                    "id": doc_id,
                    "text": doc.page_content,
                    "embedding": self.embedding_function.embed_documents(
                        [doc.page_content]
                    )[0],
                    "source": source_value,
                    "chunk_index": doc.metadata.get("chunk_index", i),
                    **dynamic_metadata,
                }
                if self._sparse_embedder is not None:
                    try:
                        sparse_vec = self._sparse_embedder.encode_document(doc.page_content)
                        entry["sparse_embedding"] = sparse_vec if sparse_vec else {}
                    except Exception:
                        logger.exception("[Milvus] 生成稀疏向量失败")
                        entry["sparse_embedding"] = {}
                data.append(entry)

            self.client.insert(
                collection_name=self.collection_name,
                data=data,
            )
            self._ensure_loaded()

            # 入库后开启 auto_load（用字符串 "true"，布尔 True 会导致 Milvus 死循环）
            try:
                self.client.alter_collection_properties(
                    collection_name=self.collection_name,
                    properties={'auto_load': 'true'},
                )
                logger.info('[Milvus] auto_load 已开启: %s', self.collection_name)
            except Exception:
                logger.exception('[Milvus] 开启 auto_load 失败')

            logger.debug(f"[Milvus] 添加文档成功，返回 ID 数量: {len(ids)}")
            return ids
        except Exception as e:
            logger.exception("[Milvus] 添加文档到向量库失败")
            raise

    def get(self, expr: str | None = None, limit: int | None = None,
            output_fields: list[str] | None = None) -> dict[str, Any]:
        try:
            filter_expr = expr if expr else ""
            if not filter_expr and not limit:
                limit = 1000
            fields = output_fields or ["*", "id", "text", "source", "chunk_index"]
            query_kwargs = {
                "collection_name": self.collection_name,
                "filter": filter_expr,
                "output_fields": fields,
            }
            if limit:
                query_kwargs["limit"] = limit
            result = self.client.query(**query_kwargs)

            documents = []
            metadatas = []
            ids = []
            for item in result:
                ids.append(item.get("id", ""))
                raw_source = item.get("source", "")
                source_value = FileMetadataExtractor.normalize_path(raw_source)
                logger.debug("[Milvus] get - raw_source: %s, normalized_source: %s", raw_source, source_value)
                base_metadata = {
                    "source": source_value,
                    "chunk_index": item.get("chunk_index", 0),
                }
                base_metadata.update(self._extract_dynamic_metadata(item))
                metadatas.append(base_metadata)
                documents.append(item.get("text", ""))

            return {
                "ids": ids,
                "metadatas": metadatas,
                "documents": documents,
            }
        except Exception as e:
            logger.warning(f"[Milvus] 获取文档失败: {e}")
            return {"ids": [], "metadatas": [], "documents": []}

    def delete(self, ids: list[str]) -> int:
        try:
            if not ids:
                return 0

            quoted = ", ".join(f'"{x}"' for x in ids)
            filter_expr = f"id in [{quoted}]"

            self.client.delete(
                collection_name=self.collection_name,
                filter=filter_expr,
            )

            return len(ids)
        except Exception as e:
            logger.warning(f"[Milvus] 删除文档失败: {e}")
            return 0

    def delete_by_metadata(self, metadata_filter: dict[str, Any]) -> int:
        filter_parts = []
        for key, value in metadata_filter.items():
            if isinstance(value, str):
                filter_parts.append(f'{key} == "{value}"')
            else:
                filter_parts.append(f"{key} == {value}")

        filter_str = " and ".join(filter_parts)

        try:
            result = self.client.delete(
                collection_name=self.collection_name,
                filter=filter_str,
            )
            return result.get("delete_count", 0) if isinstance(result, dict) else 1
        except Exception as e:
            logger.warning(f"[Milvus] 按元数据删除失败: {e}")
            return 0

    def as_retriever(self, search_kwargs: dict[str, Any]):
        return MilvusRetriever(self, search_kwargs=search_kwargs)

    def _search_raw_hits(self, query: str, k: int, expr: str | None,
                         retried: bool = False) -> list[dict[str, Any]]:
        """执行向量检索并返回原始 hit（child 粒度）。"""
        try:
            logger.debug("[Milvus] similarity_search - expr: %s", expr)
            query_embedding = self.embedding_function.embed_documents([query])[0]

            if expr:
                logger.debug("[Milvus] search expr=%s k=%d", expr, k)
                result = self.client.search(
                    collection_name=self.collection_name,
                    data=[query_embedding],
                    anns_field="embedding",
                    filter=expr,
                    limit=k,
                    output_fields=["*"],
                    search_params={"metric_type": "COSINE", "params": {}},
                )
                hit_count = sum(len(hits) for hits in result) if result else 0
                logger.debug("[Milvus] search hits=%d", hit_count)
                if result and result[0]:
                    logger.debug("[Milvus] top hit: %s", _milvus_hit_summary(result[0][0]))
                elif hit_count == 0:
                    logger.debug("[Milvus] no results for expr: %s", expr)
            else:
                result = self.client.search(
                    collection_name=self.collection_name,
                    data=[query_embedding],
                    anns_field="embedding",
                    limit=k,
                    output_fields=["*"],
                    search_params={"metric_type": "COSINE", "params": {}},
                )

            hits_flat: list[dict[str, Any]] = []
            for hits in result:
                for hit in hits:
                    hits_flat.append(hit)
            return hits_flat
        except Exception as e:
            err_msg = str(e)
            if 'not loaded' in err_msg and not retried:
                logger.info('[Milvus] 检索时 collection 未加载，正在按需加载...')
                self._ensure_loaded()
                return self._search_raw_hits(query, k, expr, retried=True)
            logger.warning(f"[Milvus] 相似性搜索失败: {e}")
            return []

    def hybrid_search(
        self,
        query: str,
        k: int = 300,
        dense_limit: int = 150,
        sparse_limit: int = 150,
        expr: str | None = None,
        as_parent: bool = False,
        expanded_query: str | None = None,
        retried: bool = False,
    ) -> list[Document]:
        """Dense + Sparse 双路独立召回，Milvus 内部 RRF 融合。"""
        if self._sparse_embedder is None:
            logger.warning("[Milvus] sparse_embedder 未配置，降级为纯 dense 检索")
            return self.similarity_search(query=query, k=k, expr=expr, as_parent=as_parent)

        try:
            from pymilvus import AnnSearchRequest, RRFRanker

            query_dense = self.embedding_function.embed_documents([query])[0]
            sparse_query_text = expanded_query or query
            query_sparse = self._sparse_embedder.encode_query(sparse_query_text)

            if not query_sparse:
                logger.debug("[Milvus] 查询稀疏向量为空，降级为纯 dense 检索")
                return self.similarity_search(query=query, k=k, expr=expr, as_parent=as_parent)

            dense_req = AnnSearchRequest(
                data=[query_dense],
                anns_field="embedding",
                param={"metric_type": "COSINE"},
                limit=dense_limit,
            )
            sparse_req = AnnSearchRequest(
                data=[query_sparse],
                anns_field="sparse_embedding",
                param={"metric_type": "IP"},
                limit=sparse_limit,
            )

            search_params: dict[str, Any] = {
                "collection_name": self.collection_name,
                "reqs": [dense_req, sparse_req],
                "ranker": RRFRanker(k=60),
                "limit": k,
                "output_fields": ["*"],
            }
            if expr:
                search_params["filter"] = expr

            result = self.client.hybrid_search(**search_params)

            hits_flat: list[dict[str, Any]] = []
            for hits in result:
                for hit in hits:
                    hits_flat.append(hit)

            logger.info(
                "[Milvus] hybrid_search: dense=%d sparse=%d -> merged=%d hits=%d",
                dense_limit,
                sparse_limit,
                k,
                len(hits_flat),
            )

            if not hits_flat:
                return []
            if as_parent and settings.RAG_A_PARENT_ENABLED:
                return self._hits_to_parent_documents(hits_flat)
            return self._hits_to_child_documents(hits_flat)
        except Exception as e:
            err_msg = str(e)
            if 'not loaded' in err_msg and not retried:
                logger.info('[Milvus] hybrid_search 时 collection 未加载，正在按需加载...')
                self._ensure_loaded()
                return self.hybrid_search(
                    query=query, k=k, dense_limit=dense_limit, sparse_limit=sparse_limit,
                    expr=expr, as_parent=as_parent, expanded_query=expanded_query, retried=True,
                )
            logger.warning(f"[Milvus] hybrid_search 失败，降级为纯 dense: {e}")
            return self.similarity_search(query=query, k=k, expr=expr, as_parent=as_parent)

    def _hits_to_child_documents(self, hits: list[dict[str, Any]]) -> list[Document]:
        documents: list[Document] = []
        for hit in hits:
            source_value = FileMetadataExtractor.normalize_path(hit.get("source", ""))
            base_metadata = {
                "id": hit.get("id", ""),
                "score": hit.get("distance", 0),
                "source": source_value,
                "chunk_index": hit.get("chunk_index", 0),
            }
            base_metadata.update(self._extract_dynamic_metadata(hit))
            documents.append(
                Document(
                    page_content=hit.get("text", ""),
                    metadata=base_metadata,
                )
            )
        return documents

    def _hits_to_parent_documents(self, hits: list[dict[str, Any]]) -> list[Document]:
        parent_best: dict[str, dict[str, Any]] = {}
        for hit in hits:
            source_value = FileMetadataExtractor.normalize_path(hit.get("source", ""))
            parent_id = str(hit.get("parent_id", "")).strip()
            if not parent_id:
                parent_id = f"legacy:{source_value}|{hit.get('chunk_index', 0)}"
            dist = float(hit.get("distance", 0) or 0.0)
            existing = parent_best.get(parent_id)
            if existing and dist >= existing["dist"]:
                continue
            parent_best[parent_id] = {
                "dist": dist,
                "hit": hit,
                "source": source_value,
            }

        parent_docs: list[Document] = []
        for parent_id, payload in parent_best.items():
            hit = payload["hit"]
            source_value = payload["source"]
            parent_chunk_index = hit.get("parent_chunk_index", hit.get("chunk_index", 0))
            parent_global_index = hit.get("parent_global_index")
            if parent_global_index is None:
                parent_global_index = parent_chunk_index
            parent_text = str(hit.get("parent_text", "") or "").strip() or hit.get(
                "text", ""
            )
            child_text = str(hit.get("text", "") or "").strip()
            if not child_text:
                child_text = parent_text
            base_metadata = {
                "id": parent_id,
                "score": payload["dist"],
                "source": source_value,
                "chunk_index": parent_global_index,
                "parent_id": parent_id,
                "parent_chunk_index": parent_chunk_index,
                "parent_global_index": parent_global_index,
                "best_child_chunk_index": hit.get(
                    "child_chunk_index", hit.get("chunk_index", 0)
                ),
                "best_child_text": child_text,
            }
            base_metadata.update(self._extract_dynamic_metadata(hit))
            parent_docs.append(Document(page_content=parent_text, metadata=base_metadata))

        parent_docs.sort(key=lambda d: float((d.metadata or {}).get("score", 0.0)))
        return parent_docs

    def similarity_search(
        self, query: str, k: int = 6, expr: str | None = None, as_parent: bool = False
    ) -> list[Document]:
        """执行相似性搜索；as_parent=True 时返回 parent 粒度。"""
        hits = self._search_raw_hits(query=query, k=k, expr=expr)
        if not hits:
            return []
        if as_parent and settings.RAG_A_PARENT_ENABLED:
            return self._hits_to_parent_documents(hits)
        return self._hits_to_child_documents(hits)

    def similarity_search_with_score(
        self, query: str, k: int = 6, expr: str | None = None
    ) -> list[tuple[Document, float]]:
        """执行相似性搜索并返回分数"""
        documents = self.similarity_search(query=query, k=k, expr=expr)
        return [(doc, doc.metadata.get("score", 0)) for doc in documents]

    def max_marginal_relevance_search(
        self,
        query: str,
        k: int = 6,
        fetch_k: int = 20,
        lambda_mult: float = 0.5,
        expr: str | None = None,
    ) -> list[Document]:
        """执行最大边际相关性搜索"""
        return self.similarity_search(query=query, k=k, expr=expr)

    def drop_collection(self):
        """删除集合"""
        try:
            if self.client.has_collection(collection_name=self.collection_name):
                self.client.drop_collection(collection_name=self.collection_name)
                logger.debug(f"[Milvus] 集合已删除: {self.collection_name}")
        except Exception as e:
            logger.error(f"[Milvus] 删除集合失败: {str(e)}")
            raise


class MilvusRetriever:
    """Milvus 检索器"""

    def __init__(self, store: MilvusVectorStore, search_kwargs: dict[str, Any]):
        self.store = store
        self.search_kwargs = search_kwargs

    def invoke(self, query: str) -> list[Document]:
        """检索相关文档"""
        k = self.search_kwargs.get("k", 6)
        expr = self.search_kwargs.get("expr")
        fetch_k = self.search_kwargs.get("fetch_k", 20)
        lambda_mult = self.search_kwargs.get("lambda_mult", 0.5)
        search_type = self.search_kwargs.get("search_type", "similarity")
        as_parent = bool(self.search_kwargs.get("as_parent", False))

        if search_type == "mmr":
            return self.store.max_marginal_relevance_search(
                query=query,
                k=k,
                fetch_k=fetch_k,
                lambda_mult=lambda_mult,
                expr=expr,
            )
        else:
            return self.store.similarity_search(
                query=query, k=k, expr=expr, as_parent=as_parent
            )


class VectorStoreManager:
    """向量存储管理器，单 collection (rag_docs) 多 group_id 通过 metadata 过滤"""

    SPARSE_VOCAB_DIR: str = str(_project_root / "data")

    def __init__(self):
        self._vector_store: MilvusVectorStore | None = None
        self._sparse_embedder: object | None = None

    def _get_sparse_embedder(self):
        """获取或创建 SparseEmbedder，优先从文件加载。"""
        if self._sparse_embedder is not None:
            return self._sparse_embedder

        from repository.vector_store.sparse_embedder import SparseEmbedder

        se = SparseEmbedder()
        vocab_path = Path(self.SPARSE_VOCAB_DIR) / "sparse_vocab.pkl"

        if not se.load(str(vocab_path)):
            logger.debug("[VectorStore] SparseEmbedder 词汇表不存在，待入库时构建: %s", vocab_path)
        else:
            logger.info("[VectorStore] SparseEmbedder 已加载: %s", vocab_path)

        self._sparse_embedder = se
        return se

    def _get_embeddings_from_config(self):
        """从配置创建嵌入模型"""
        api_key = settings.EMBEDDING_API_KEY

        model_kwargs = {}

        return OpenAIEmbeddings(
            base_url=settings.EMBEDDING_BASE_URL,
            model=settings.EMBEDDING_MODEL,
            api_key=SecretStr(api_key),
            model_kwargs=model_kwargs,
            chunk_size=100,
            show_progress_bar=False,
        )

    def get_vector_store(self, group_id: str = "") -> MilvusVectorStore:
        """获取单例向量存储（所有 group 共用 rag_docs collection，通过 metadata.group_id 区分）。"""
        if self._vector_store is not None:
            return self._vector_store

        logger.debug("[VectorStore] 创建新的嵌入模型...")
        embeddings = self._get_embeddings_from_config()
        logger.debug("[VectorStore] 嵌入模型创建成功")

        self._vector_store = MilvusVectorStore(
            collection_name=COLLECTION_NAME,
            embedding_function=embeddings,
            sparse_embedder=self._get_sparse_embedder(),
        )
        logger.debug("[VectorStore] 向量库创建成功: %s", COLLECTION_NAME)
        return self._vector_store

    @staticmethod
    def _escape_expr_value(value: str) -> str:
        return re.sub(r'(["\\])', r"\\\1", value or "")

    def add_file(self, file_path: str, group_id: str, open_id: str = "") -> int:
        logger.info("[AddFile] 开始添加文件: path=%s, group_id=%s, open_id=%s", file_path, group_id, open_id)

        try:
            splits = splitter.load_and_split_for_index(file_path)
            logger.debug("[AddFile] 切分后文档数量: %d", len(splits) if splits else 0)
            if not splits:
                logger.warning("[AddFile] 未加载到任何文档或文档切分后为空")
                return 0
        except Exception as e:
            logger.exception("[AddFile] 加载或切分文档失败")
            raise

        seen = set()
        unique_splits = []
        file_name = Path(file_path).name
        for doc in splits:
            key = doc.page_content.strip().replace("\r\n", "\n").replace("\r", "\n")
            if key and key not in seen:
                seen.add(key)
                doc.metadata["file_name"] = file_name
                doc.metadata["group_id"] = group_id
                if open_id:
                    doc.metadata["uploaded_by"] = open_id
                unique_splits.append(doc)
        logger.debug("[AddFile] 去重后文档数量: %d", len(unique_splits))
        if not unique_splits:
            logger.warning("[AddFile] 去重后文档为空")
            return 0

        logger.info("[AddFile] 获取向量库: group_id=%s", group_id)
        store = self.get_vector_store(group_id=group_id)
        logger.debug("[AddFile] 向量库获取成功，开始添加文档...")

        se = self._sparse_embedder
        Path(self.SPARSE_VOCAB_DIR).mkdir(parents=True, exist_ok=True)
        vocab_path = str(Path(self.SPARSE_VOCAB_DIR) / "sparse_vocab.pkl")
        was_empty = se is not None and se.vocab_size == 0
        if was_empty:
            texts = [d.page_content for d in unique_splits]
            logger.info("[AddFile] 首次入库，构建 SparseEmbedder 词汇表")
            se.fit(texts)
            se.save(vocab_path)

        try:
            new_ids = store.add_documents(unique_splits)
            logger.info("[AddFile] 文件添加成功: chunk数量=%d", len(unique_splits))

            # 覆盖上传场景：清理该文件旧版本的 chunks（保留刚插入的新 chunks）
            if new_ids:
                normalized_source = FileMetadataExtractor.normalize_path(str(Path(file_path).resolve()))
                escaped_source = self._escape_expr_value(normalized_source)
                escaped_group = self._escape_expr_value(group_id)
                filter_expr = f'source == "{escaped_source}" && group_id == "{escaped_group}"'
                try:
                    existing = store.client.query(
                        collection_name=store.collection_name,
                        filter=filter_expr,
                        output_fields=["id"],
                    )
                    all_ids = {item["id"] for item in existing}
                    old_ids = [oid for oid in all_ids if oid not in set(new_ids)]
                    if old_ids:
                        from itertools import islice

                        def batched(iterable, n):
                            it = iter(iterable)
                            while batch := list(islice(it, n)):
                                yield batch

                        for batch in batched(old_ids, 100):
                            ids_str = ", ".join(f'"{x}"' for x in batch)
                            store.client.delete(
                                collection_name=store.collection_name,
                                filter=f"id in [{ids_str}]",
                            )
                        logger.info("[AddFile] 已清理旧版本 chunks: %d", len(old_ids))
                except Exception:
                    logger.exception("[AddFile] 清理旧版本 chunks 失败")

            if se is not None and not was_empty:
                try:
                    se.add_documents([d.page_content for d in unique_splits])
                    se.save(vocab_path)
                except Exception:
                    logger.exception("[AddFile] SparseEmbedder 增量更新失败")
        except Exception as e:
            logger.exception("[AddFile] 添加文档到向量库失败")
            raise

        return len(unique_splits)

    def delete_file_and_vectors(self, file_path: str, group_id: str) -> tuple:
        logger.info("[DeleteFile] 开始删除文件及向量: path=%s, group_id=%s", file_path, group_id)
        path = Path(file_path)
        path_str = str(path.resolve())
        path_str_normalized = FileMetadataExtractor.normalize_path(path_str)

        store = self.get_vector_store(group_id=group_id)

        # 空 collection 跳过 Milvus 删除（delete 需要 collection 已加载）
        delete_count = 0
        try:
            stats = store.client.get_collection_stats(collection_name=store.collection_name)
            row_count = stats.get('row_count', 0) if isinstance(stats, dict) else 0
        except Exception:
            row_count = 0

        if row_count > 0:
            store._ensure_loaded()
            escaped_source = self._escape_expr_value(path_str_normalized)
            escaped_group = self._escape_expr_value(group_id)
            filter_expr = f'source == "{escaped_source}" && group_id == "{escaped_group}"'

            try:
                result = store.client.delete(
                    collection_name=store.collection_name,
                    filter=filter_expr,
                )
                delete_count = result.get("delete_count", 0) if isinstance(result, dict) else 0
            except Exception as e:
                logger.warning("[DeleteFile] 按 source+group_id 删除失败: %s", e)
        else:
            logger.info("[DeleteFile] collection 为空，跳过向量删除")

        n = delete_count
        file_deleted = False
        try:
            if path.exists() and path.is_file():
                path.unlink()
                file_deleted = True
        except Exception as e:
            logger.error("[DeleteFile] 删除磁盘文件失败: path=%s, error=%s", path, str(e))
            raise

        stem = path.stem
        try:
            embedded_root = Path(settings.EMBEDDED_DIR).resolve()
            chunk_dir = (embedded_root / _safe_group_dir(group_id) / stem).resolve()
            chunk_dir.relative_to(embedded_root)
            if chunk_dir.is_dir():
                shutil.rmtree(chunk_dir)
                logger.info("[DeleteFile] 已删除 embedded 切分目录: %s", chunk_dir)
            else:
                logger.debug("[DeleteFile] embedded 切分目录不存在，跳过: %s", chunk_dir)
        except ValueError:
            logger.warning(
                "[DeleteFile] embedded 路径不在 EMBEDDED_DIR 下，已跳过: group_id=%s stem=%s",
                group_id,
                stem,
            )
        except Exception as e:
            logger.error(
                "[DeleteFile] 删除 embedded 目录失败: group_id=%s stem=%s error=%s",
                group_id,
                stem,
                str(e),
            )

        logger.info(
            "[DeleteFile] 删除完成: normalized_source=%s, vectors_deleted=%d, file_deleted=%s, group_id=%s",
            path_str_normalized,
            n,
            file_deleted,
            group_id,
        )
        return (n, file_deleted)

    def get_vector_store_info(self, group_id: str, expr: str | None = None) -> dict:
        try:
            vector_store = self.get_vector_store(group_id=group_id)
            escaped_group = self._escape_expr_value(group_id)
            group_filter = f'group_id == "{escaped_group}"'
            combined = f"{group_filter} && ({expr})" if expr else group_filter
            all_docs = vector_store.get(expr=combined, output_fields=["source", "file_name"])
            sources = all_docs.get("metadatas", [])
            source_counts: dict[str, int] = {}
            for source in sources:
                source_path = source.get("source", "unknown")
                source_path_normalized = FileMetadataExtractor.normalize_path(source_path)
                source_counts[source_path_normalized] = source_counts.get(source_path_normalized, 0) + 1
            return {
                "total_chunks": len(sources),
                "sources": source_counts,
            }
        except Exception as e:
            logger.error("[Store] 获取向量库信息失败: %s", str(e))
            return {"error": str(e)}

    def clear_vector_store(self, group_id: str):
        if not group_id or not group_id.strip():
            raise ValueError("group_id 不能为空")

        store = self.get_vector_store(group_id=group_id)
        escaped_group = self._escape_expr_value(group_id)
        filter_expr = f'group_id == "{escaped_group}"'
        try:
            result = store.client.delete(
                collection_name=store.collection_name,
                filter=filter_expr,
            )
            logger.info(
                "[VectorStore] 已清除 group_id=%s 的向量, delete_count=%s",
                group_id,
                result.get("delete_count", "?") if isinstance(result, dict) else "?",
            )
        except Exception as e:
            logger.error("[VectorStore] 清除向量失败: %s", e)

    def get_all_vector_stores(self) -> dict[str, MilvusVectorStore]:
        """获取所有向量存储实例（单 collection 模式始终返回同一个）"""
        if self._vector_store is None:
            return {}
        return {COLLECTION_NAME: self._vector_store}

    def reset_for_child(self):
        """fork 后子进程重置：清空缓存的 vector store 和 sparse embedder，下次使用自动重建"""
        self._vector_store = None
        self._sparse_embedder = None


def export_chunks_to_folder(file_path: str, output_dir: str = EXPORT_CHUNKS_DIR) -> int:
    logger.info("[ExportChunks] 开始导出切分结果: file_path=%s, output_dir=%s", file_path, output_dir)
    path = Path(file_path)
    splits = splitter.load_and_split_for_export(str(path))
    if not splits:
        logger.info("[ExportChunks] 无可导出文档: file_path=%s", file_path)
        return 0
    if not splits:
        logger.info("[ExportChunks] 文档切分后为空: file_path=%s", file_path)
        return 0
    safe_name = path.stem
    out_subdir = Path(output_dir) / safe_name
    out_subdir.mkdir(parents=True, exist_ok=True)
    for i, doc in enumerate(splits):
        chunk_path = out_subdir / f"chunk_{i + 1:04d}.txt"
        chunk_path.write_text(doc.page_content, encoding="utf-8")
    logger.info("[ExportChunks] 导出完成: output_subdir=%s, chunks=%d", out_subdir, len(splits))
    return len(splits)


_vector_store_manager = VectorStoreManager()


def reset_for_child():
    """fork 后子进程重置 vector store 管理器"""
    _vector_store_manager.reset_for_child()


get_vector_store = _vector_store_manager.get_vector_store
add_file = _vector_store_manager.add_file
delete_file_and_vectors = _vector_store_manager.delete_file_and_vectors
get_vector_store_info = _vector_store_manager.get_vector_store_info
clear_vector_store = _vector_store_manager.clear_vector_store
