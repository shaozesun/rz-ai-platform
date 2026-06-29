"""
稀疏向量编码器：基于 BM25 的文本 → Milvus SPARSE_FLOAT_VECTOR 编码。

文档侧：{token_id: normalized_tf}  (词频 / 文档长度)
查询侧：{token_id: idf}            (逆文档频率)

IP (内积) = Σ tf_norm * idf ≈ BM25 简化分，用于 Milvus hybrid_search。
"""
from __future__ import annotations

import logging
import math
import pickle
import re
from collections import Counter
from pathlib import Path

logger = logging.getLogger(__name__)

try:
    import jieba
except ImportError:  # pragma: no cover
    jieba = None


def _tokenize(text: str) -> list[str]:
    """
    三路分词，与 RagService._tokenize_sparse 保持一致：
    1. 英文/缩写 regex 提取
    2. jieba 中文分词
    3. 字符级 2~4 gram 兜底
    """
    lower = (text or "").strip().lower()
    if not lower:
        return []
    tokens: list[str] = re.findall(r"[a-z0-9_./:-]{2,}", lower)
    chinese_groups: list[str] = re.findall(r"[\u4e00-\u9fff]+", lower)
    if not chinese_groups:
        return _dedupe(tokens)

    jieba_tokens: list[str] = []
    if jieba is not None:
        for grp in chinese_groups:
            try:
                jieba_tokens.extend([w for w in jieba.lcut(grp) if len(w.strip()) >= 2])
            except Exception:
                pass

    gram_tokens: list[str] = []
    for group in chinese_groups:
        n = len(group)
        for gram_len in range(2, min(4, n) + 1):
            for i in range(0, n - gram_len + 1):
                gram_tokens.append(group[i: i + gram_len])

    return _dedupe(tokens + jieba_tokens + gram_tokens)


def _dedupe(tokens: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for token in tokens:
        t = (token or "").strip()
        if not t or t in seen:
            continue
        seen.add(t)
        out.append(t)
    return out


class SparseEmbedder:
    """BM25 稀疏向量编码器。

    通过 fit() 从语料构建词汇表，encode_document() / encode_query()
    分别生成 Milvus 兼容的 SPARSE_FLOAT_VECTOR dict。
    """

    def __init__(self) -> None:
        self._token_to_id: dict[str, int] = {}
        self._df: dict[int, int] = {}      # 原始文档频率（用于增量更新）
        self._idf: dict[int, float] = {}
        self._n_docs: int = 0

    # ------------------------------------------------------------------
    # 词汇表构建
    # ------------------------------------------------------------------

    def fit(self, corpus: list[str]) -> None:
        """从全量文档构建词汇表 + IDF。

        Args:
            corpus: 所有文档的文本列表。
        """
        if not corpus:
            logger.warning("[SparseEmbedder] fit 收到空语料，跳过")
            return

        df: Counter[int] = Counter()
        doc_count = 0

        for text in corpus:
            tokens = _tokenize(text)
            if not tokens:
                continue
            doc_count += 1
            for token in set(tokens):
                tid = self._token_to_id.setdefault(token, len(self._token_to_id))
                df[tid] += 1

        self._n_docs = doc_count
        self._df.clear()
        self._idf.clear()
        for tid, cnt in df.items():
            self._df[tid] = cnt
            self._idf[tid] = math.log(1.0 + (doc_count - cnt + 0.5) / (cnt + 0.5))

        logger.info(
            "[SparseEmbedder] 词汇表构建完成: tokens=%d docs=%d",
            len(self._token_to_id),
            doc_count,
        )

    def add_documents(self, texts: list[str]) -> None:
        """增量添加文档，更新 DF 并重新计算 IDF。

        用于新文档入库后在线更新词汇表，无需重新 fit。
        """
        for text in texts:
            tokens = _tokenize(text)
            if not tokens:
                continue
            self._n_docs += 1
            for token in set(tokens):
                tid = self._token_to_id.setdefault(token, len(self._token_to_id))
                self._df[tid] = self._df.get(tid, 0) + 1

        # 从 DF 重新计算 IDF
        for tid, cnt in self._df.items():
            self._idf[tid] = math.log(
                1.0 + (self._n_docs - cnt + 0.5) / (cnt + 0.5)
            )

    # ------------------------------------------------------------------
    # 编码
    # ------------------------------------------------------------------

    def encode_document(self, text: str) -> dict[int, float]:
        """文档侧编码：{token_id: normalized_tf}。

        规范化值 = term_frequency / doc_length，使内积接近 BM25 行为。
        """
        tokens = _tokenize(text)
        if not tokens:
            return {}
        tf = Counter(tokens)
        doc_len = float(len(tokens))
        result: dict[int, float] = {}
        for token, count in tf.items():
            tid = self._token_to_id.get(token)
            if tid is None:
                continue
            result[tid] = count / doc_len
        return result

    def encode_query(self, text: str) -> dict[int, float]:
        """查询侧编码：{token_id: idf}。

        接受扩展后的 query（含同义词），生成 IDF 权重。
        """
        tokens = _tokenize(text)
        if not tokens:
            return {}
        qtf = Counter(tokens)
        result: dict[int, float] = {}
        for token, count in qtf.items():
            tid = self._token_to_id.get(token)
            if tid is None:
                continue
            idf = self._idf.get(tid, 0.0)
            if idf <= 0.0:
                continue
            result[tid] = float(count) * idf
        return result

    # ------------------------------------------------------------------
    # 持久化
    # ------------------------------------------------------------------

    def save(self, path: str) -> None:
        """序列化词汇表到 pickle 文件。"""
        data = {
            "token_to_id": self._token_to_id,
            "df": self._df,
            "idf": self._idf,
            "n_docs": self._n_docs,
        }
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as f:
            pickle.dump(data, f, protocol=pickle.HIGHEST_PROTOCOL)
        logger.info("[SparseEmbedder] 词汇表已保存: %s (tokens=%d)", path, len(self._token_to_id))

    def load(self, path: str) -> bool:
        """从 pickle 文件加载词汇表。"""
        p = Path(path)
        if not p.exists():
            logger.debug("[SparseEmbedder] 词汇表文件不存在: %s", path)
            return False
        try:
            with open(path, "rb") as f:
                data = pickle.load(f)
            self._token_to_id = data["token_to_id"]
            self._n_docs = data.get("n_docs", 0)
            # _df 可能不存在于旧格式 pickle 文件中
            self._df = data.get("df", {})
            self._idf = data["idf"]
            # 旧格式 pickle 缺少 _df 时，从 _idf 近似重建
            if not self._df and self._idf and self._n_docs > 0:
                logger.info("[SparseEmbedder] 旧格式词汇表，从 IDF 近似重建 DF (docs=%d)", self._n_docs)
                for tid, idf_val in self._idf.items():
                    if idf_val > 0:
                        raw_df = max(1, int((self._n_docs + 1.0) / math.exp(idf_val) - 0.5))
                        self._df[tid] = raw_df
            logger.info(
                "[SparseEmbedder] 词汇表已加载: %s (tokens=%d docs=%d)",
                path,
                len(self._token_to_id),
                self._n_docs,
            )
            return True
        except Exception:
            logger.exception("[SparseEmbedder] 加载词汇表失败: %s", path)
            return False

    @property
    def vocab_size(self) -> int:
        return len(self._token_to_id)

    @property
    def doc_count(self) -> int:
        return self._n_docs
