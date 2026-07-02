"""
RAG 服务：统一检索链（2a/2b/2c + 融合 + rerank）并保留上传/删除能力。
"""
from __future__ import annotations

import logging
import math
import os
import re
import threading
import urllib.parse
from collections import Counter
from pathlib import Path
from typing import Any, AsyncIterator

from langchain_core.documents import Document
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import Runnable, RunnableLambda
from langchain_core.runnables.history import RunnableWithMessageHistory
from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from config.settings import settings
from service.rag.utils.metrics import rag_stage
from repository.vector_store import milvus_store as store
from service.rag.conversation.chat_history import get_session_history

logger = logging.getLogger(__name__)
_project_root = Path(__file__).resolve().parent.parent.parent
try:
    import jieba  # type: ignore
except Exception:  # pragma: no cover
    jieba = None

# 需要触发 query rewrite 的指代词/省略句模式
_REWRITE_TRIGGER_PATTERN = re.compile(
    r"它[们]?|其|这[个些种]?|那[个些种]?|该[设备系统流程项]?|"
    r"上[面述文次]|前[面述文]|刚[才刚]|之[前后]|"
    r"怎么[样么做办]|如何|什么[时候]?|哪[个些]"
)

# 只有匹配到指代词才触发改写

# 简单问候/闲聊关键词（跳过 RAG 检索，直接由 LLM 自由回答）
_GREETING_PATTERNS = [
    r"^(你好|您好|hi|hello|嗨|早|晚上好|下午好|早上好)[\s，。,\.!！?？]*$",
    r"^(谢谢|感谢|多谢|3Q|thx|thanks|thank\s*you)[\s，。,\.!！?？]*$",
    r"^(你是谁|你叫什么|你能做什么|你会什么|介绍一下自己|你是干嘛的)[\s，。,\.!！?？]*$",
    r"^(在吗|在不在|有人吗|hello\??|hi\??)$",
]
_GREETING_RE = re.compile("|".join(_GREETING_PATTERNS), re.IGNORECASE)


class RagService:
    _instance: "RagService | None" = None
    _lock = threading.Lock()
    _initialized: bool = False
    _chain: Runnable | None = None
    _load_chain: bool = False

    def __new__(cls, load_chain: bool = False):
        if RagService._instance is None:
            with cls._lock:
                if RagService._instance is None:
                    instance = super().__new__(cls)
                    RagService._initialized = False
                    RagService._instance = instance
        if not RagService._initialized:
            RagService._chain = None
            RagService._load_chain = load_chain
            RagService._initialized = True
        return RagService._instance

    def __init__(self, load_chain: bool = False):
        if RagService._initialized:
            return
        RagService._chain = None
        RagService._load_chain = load_chain

    @classmethod
    def reset_for_child(cls):
        """fork 后子进程重置单例：替换锁对象（旧锁可能处于死锁状态），清空实例和 chain"""
        cls._lock = threading.Lock()
        cls._instance = None
        cls._initialized = False
        cls._chain = None

    @staticmethod
    def _normalize_path(path: str) -> str:
        if not path:
            return path
        decoded = urllib.parse.unquote(path)
        return decoded.replace("\\", "/")

    @staticmethod
    def _short_file_label(metadata: dict | None, source: str = "") -> str:
        md = metadata or {}
        name = str(md.get("file_name", "")).strip()
        if name:
            return name
        src = str(md.get("source", source) or "").strip()
        return Path(src).name if src else "unknown"

    @staticmethod
    def _embedded_chunk_label(metadata: dict | None) -> str:
        """与 embedded 目录 chunk_XXXX.txt 对齐的编号标签。"""
        md = metadata or {}
        raw = md.get("parent_global_index", md.get("chunk_index", None))
        if raw is None or raw == "":
            return "chunk_????"
        try:
            return f"chunk_{int(raw) + 1:04d}"
        except (TypeError, ValueError):
            return f"chunk_{raw}"

    @staticmethod
    def _format_chunk_refs(
        docs: list[Document],
        fusion_scores: dict[str, dict[str, float]],
        *,
        score_key: str = "s_final",
    ) -> str:
        lines = RagService._format_chunk_ref_lines(docs, fusion_scores, score_key=score_key)
        return ", ".join(lines) if lines else "-"

    @staticmethod
    def _format_chunk_ref_lines(
        docs: list[Document],
        fusion_scores: dict[str, dict[str, float]],
        *,
        score_key: str = "s_final",
    ) -> list[str]:
        lines: list[str] = []
        for doc in docs:
            md = doc.metadata or {}
            label = RagService._short_file_label(md)
            chunk_label = RagService._embedded_chunk_label(md)
            score = float(fusion_scores.get(RagService._doc_key(doc), {}).get(score_key, 0.0))
            lines.append(f"{label}#{chunk_label} ({score:.3f})")
        return lines

    @staticmethod
    def _extract_question_text(value: Any) -> str:
        """从 str / dict / LangChain Message 等输入中提取纯文本问题。"""
        if value is None:
            return ""
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, dict):
            for key in ("question", "input", "text", "query"):
                if key in value and value[key] is not None:
                    return RagService._extract_question_text(value[key])
            return ""
        if isinstance(value, list):
            for item in reversed(value):
                text = RagService._extract_question_text(item)
                if text:
                    return text
            return ""
        content = getattr(value, "content", None)
        if content is not None:
            if isinstance(content, str):
                return content.strip()
            if isinstance(content, list):
                parts: list[str] = []
                for part in content:
                    if isinstance(part, str):
                        parts.append(part)
                    elif isinstance(part, dict) and part.get("type") == "text":
                        parts.append(str(part.get("text", "")))
                return " ".join(parts).strip()
            return str(content).strip()
        return str(value).strip()

    @staticmethod
    def _is_greeting_or_chitchat(question: str) -> bool:
        """判断是否简单问候或闲聊，不需要知识库检索。"""
        q = (question or "").strip()
        if not q:
            return True
        if len(q) <= 8 and _GREETING_RE.match(q):
            return True
        return False

    @staticmethod
    def _needs_rewrite(question: str) -> bool:
        """仅当问题包含指代词/省略模式时才触发改写，避免短查询被错误扩写。"""
        q = (question or "").strip()
        if not q:
            return False
        return bool(_REWRITE_TRIGGER_PATTERN.search(q))

    def _rewrite_query(
        self, question: str, history_messages: list, llm=None
    ) -> str:
        """利用对话历史将含指代/省略的追问改写为独立检索查询。"""
        if not history_messages:
            return question

        # 取最近 6 条消息（3 轮对话）
        recent = history_messages[-6:]
        history_lines: list[str] = []
        for msg in recent:
            role = "用户" if getattr(msg, "type", "") == "human" else "助手"
            content = getattr(msg, "content", "")
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                text = " ".join(
                    str(p.get("text", p) if isinstance(p, dict) else p)
                    for p in content
                )
            else:
                text = str(content)
            if text.strip():
                history_lines.append(f"{role}: {text.strip()[:300]}")

        if not history_lines:
            return question

        history_text = "\n".join(history_lines)
        rewrite_prompt = f"""根据对话历史，将用户当前问题中的指代词（如"它""这个""该"）替换为具体的实体名称。
只做指代消解，不要自行添加、扩展或推测任何检索词。
如果当前问题已经是完整独立的查询，请原样输出，不要做任何修改。
只输出改写后的查询文本，不要输出任何解释、标点或引号。

对话历史：
{history_text}

当前问题：{question}
改写结果："""

        if llm is None:
            llm = self._get_llm(temperature=0.0)
        try:
            response = llm.invoke(rewrite_prompt)
            rewritten = (response.content if hasattr(response, "content") else str(response)).strip()
            if rewritten and rewritten != question:
                logger.debug(
                    "[QueryRewrite] 改写前=%s 改写后=%s",
                    question,
                    rewritten,
                )
                return rewritten
        except Exception:
            logger.exception("[QueryRewrite] 改写失败，降级使用原问题")

        return question

    def _classify_intent(self, question: str) -> dict[str, str]:
        """将用户问题分类到操作类型和子系统，用于知识源路由。

        Returns:
            {"doc_type": "SOP|EOP|MOP|general",
             "subsystem": "供配电|制冷|智能化|general"}
        """
        classify_prompt = f"""分析以下用户问题，判断它属于哪类操作文档和哪个子系统。
输出严格 JSON 格式：{{"doc_type":"...","subsystem":"..."}}

操作类型（doc_type）：
- SOP：标准操作、日常操作、启动/停止/切换/开关机流程
- EOP：应急处理、故障处理、事故处理、紧急操作、报警处理
- MOP：维护保养、检修、巡检、定期检查、季检/年检/月检
- general：无法明确归类

子系统（subsystem）：
- 供配电：UPS、发电机、配电柜、变压器、ATS、电池、直流屏、高低压
- 制冷：冷机、冷却塔、精密空调、CDU、水泵、板换、冷却水、冷冻水
- 智能化：消防、门禁、监控、报警、安防、烟感、极早期、广播
- general：无法明确归类

用户问题：{question}
JSON 输出："""
        try:
            llm = self._get_llm(temperature=0.0)
            response = llm.invoke(classify_prompt)
            raw = (response.content if hasattr(response, "content") else str(response)).strip()
            # 从 LLM 输出中提取 JSON
            import json as _json
            match = re.search(r"\{[^}]+\}", raw)
            if match:
                result = _json.loads(match.group(0))
                doc_type = str(result.get("doc_type", "general")).strip()
                subsystem = str(result.get("subsystem", "general")).strip()
                valid_types = {"SOP", "EOP", "MOP", "general"}
                valid_subsystems = {"供配电", "制冷", "智能化", "general"}
                if doc_type not in valid_types:
                    doc_type = "general"
                if subsystem not in valid_subsystems:
                    subsystem = "general"
                logger.info(
                    "[IntentClassify] q=%s → doc_type=%s subsystem=%s",
                    question[:100],
                    doc_type,
                    subsystem,
                )
                return {"doc_type": doc_type, "subsystem": subsystem}
        except Exception:
            logger.exception("[IntentClassify] 分类失败，降级为 general")
        return {"doc_type": "general", "subsystem": "general"}

    @staticmethod
    def _intent_to_milvus_filter(intent: dict[str, str]) -> str | None:
        """将意图分类结果转换为 Milvus metadata 过滤表达式。

        使用 in 同时匹配具体类型和 "general"，确保无元数据标记的文件也能被检索。
        """
        parts: list[str] = []
        doc_type = intent.get("doc_type", "general")
        subsystem = intent.get("subsystem", "general")
        if doc_type != "general":
            parts.append(f'doc_type in ["{doc_type}", "general"]')
        if subsystem != "general":
            parts.append(f'subsystem in ["{subsystem}", "general"]')
        if not parts:
            return None
        return " && ".join(parts)

    @staticmethod
    def _boilerplate_penalty(text: str) -> float:
        """检测文档元数据片段（修订记录/编写目的/版权声明/xlsx SOP前置页等），返回惩罚系数。"""
        head = text[:400]
        patterns = [
            # PDF 文档模板
            "编写目的", "文档编号", "文档版本", "文档日期",
            "版权声明", "拟制", "会签", "审批", "标准化",
            "修改记录", "修改日期", "批准人", "内部公开",
            # xlsx/SOP 前置元数据
            "文件编号", "设备信息", "设备厂家", "设备型号",
            "现场信息", "数据中心名称", "适用场景", "售后联系电话",
        ]
        hits = sum(1 for p in patterns if p in head)
        # 页码模式："第 X 页共 Y 页"
        if re.search(r"第\s*\d+\s*页\s*共\s*\d+\s*页", head):
            hits += 1
        # xlsx 结构化章节编号连续出现（如"第01部分""第02部分"）
        part_ids = re.findall(r"第\d+部分", head)
        if len(part_ids) >= 2:
            hits += len(part_ids) - 1
        if hits >= 4:  return 0.20
        if hits >= 3:  return 0.35
        if hits >= 2:  return 0.55
        return 1.0

    @staticmethod
    def _is_metadata_chunk(text: str) -> bool:
        """硬过滤：识别纯粹的文档元数据/扉页/超短片段，直接丢弃而非降权。"""
        # 清除页眉页脚噪声后长度不足 → 无实质内容
        cleaned = re.sub(r"【[^】]*】", "", text or "")
        cleaned = re.sub(r"第\s*\d+\s*页\s*共\s*\d+\s*页", "", cleaned)
        cleaned = cleaned.strip()
        if len(cleaned) < 40:
            return True
        head = text[:500]
        # 强烈元数据信号：5+ 个 boilerplate 模式命中
        strong_patterns = [
            "编写目的", "文档编号", "文档版本", "文档日期",
            "版权声明", "拟制", "会签", "审批", "标准化",
            "修改记录", "修改日期", "批准人", "内部公开",
            "文件编号", "设备信息", "设备厂家", "设备型号",
            "现场信息", "数据中心名称", "适用场景", "售后联系电话",
        ]
        hits = sum(1 for p in strong_patterns if p in head)
        part_ids = re.findall(r"第\d+部分", head)
        if len(part_ids) >= 2:
            hits += len(part_ids) - 1
        if re.search(r"第\s*\d+\s*页\s*共\s*\d+\s*页", head):
            hits += 1
        if hits >= 5:
            return True
        # 纯修订记录块：有修改记录 + 版本号 + 日期
        if "修改记录" in head and re.search(r"V\d+\.\d+", head) and re.search(r"\d{4}[./-]\d{1,2}", head):
            return True
        # 纯 xlsx 前置表格：文件编号 + 多个设备字段 + 没有实质内容指示词
        content_indicators = ["操作步骤", "处理方法", "应急流程", "执行", "确认", "检查", "通知", "通报"]
        has_content = any(w in text for w in content_indicators)
        if not has_content and hits >= 3:
            return True
        return False

    @staticmethod
    def _doc_name_relevance(question: str, doc: Document) -> float:
        """文档文件名与 query 的词面重叠度，返回 [1.0, 1.15] 的乘性系数。

        当 query 与文档名共享关键词时加分（如 query 含"事件"，
        文档名含"事件管理流程"），基于 jieba 分词后的词面重叠数。
        """
        md = doc.metadata or {}
        file_name = str(md.get("file_name", "")).strip()
        if not file_name:
            return 1.0
        # 去扩展名、去数字编号路径、去公司前缀
        name = re.sub(r"\.\w+$", "", file_name)
        name = re.sub(r"^[\d.\-]+", "", name)
        name = re.sub(r"润泽科技[-\u4e00-\u9fa5]*-[A-Z]+-?", "", name)
        if not name.strip():
            return 1.0

        if jieba is not None:
            q_words = set(w for w in jieba.lcut(question) if len(w) >= 2)
            n_words = set(w for w in jieba.lcut(name) if len(w) >= 2)
        else:
            # 中文无空格，退化为单字粒度
            q_words = set(question)
            n_words = set(name)

        overlap = q_words & n_words
        n_overlap = len(overlap)

        base = 1.0

        if n_overlap >= 3:
            base *= 1.12
        elif n_overlap >= 2:
            base *= 1.08
        elif n_overlap >= 1:
            base *= 1.04
        return min(base, 1.15)

    def _resolve_file_name(self, doc: Document, source: str = "") -> str:
        md = doc.metadata or {}
        src = source or str(md.get("source", ""))
        return str(md.get("file_name", "")).strip() or self._short_file_label(md, src)

    def _count_file_in_docs(self, docs: list[Document], file_name: str) -> int:
        return sum(1 for d in docs if self._resolve_file_name(d) == file_name)

    def _can_add_final_doc(
        self,
        doc: Document,
        *,
        source_counter: dict[str, int],
        file_counter: dict[str, int],
        per_source_cap: int,
        per_file_cap: int,
    ) -> bool:
        md = doc.metadata or {}
        source = str(md.get("source", ""))
        file_name = self._resolve_file_name(doc, source)
        if source_counter.get(source, 0) >= per_source_cap:
            return False
        if per_file_cap > 0 and file_counter.get(file_name, 0) >= per_file_cap:
            return False
        return True

    def _register_final_doc(
        self,
        doc: Document,
        *,
        source_counter: dict[str, int],
        file_counter: dict[str, int],
    ) -> None:
        md = doc.metadata or {}
        source = str(md.get("source", ""))
        file_name = self._resolve_file_name(doc, source)
        source_counter[source] = source_counter.get(source, 0) + 1
        file_counter[file_name] = file_counter.get(file_name, 0) + 1

    def _build_final_selection(
        self,
        question: str,
        scored: list[tuple[float, float, Document]],
        pool_docs: list[Document],
        fusion_scores: dict[str, dict[str, float]],
        limit: int,
    ) -> list[Document]:
        """
        先检查 #1 与 #2 的分数差距是否超过阈值（RAG_PRIMARY_GAP_THRESHOLD）：
        - 差距足够大（如 0.12）→ 触发主文件补全，从 #1 所在文件多取 N 条
        - 差距太小（如 0.006）→ 跳过主文件补全，退化为纯全局排序 + 多样性 cap
        最后按 rerank_score 从高补足至 limit。
        """
        if not scored or limit <= 0:
            return []

        per_source_cap = settings.RAG_PER_SOURCE_CAP
        per_file_cap = settings.RAG_PER_FILE_CAP
        primary_n = max(0, int(settings.RAG_PRIMARY_FILE_CHUNKS))

        # 主文件补全触发条件：#1 与 #2 的分数差距需超过阈值
        # 避免微弱分数差异（如 0.755 vs 0.749）被放大为主文件级别偏差
        gap_threshold = float(getattr(settings, "RAG_PRIMARY_GAP_THRESHOLD", 0.075))
        if primary_n > 0 and len(scored) >= 2:
            gap = scored[0][0] - scored[1][0]
            if gap < gap_threshold:
                if logger.isEnabledFor(20):  # DEBUG
                    logger.debug(
                        "[RAG] 主文件补全跳过: gap=%.4f < threshold=%.3f, "
                        "s1=%.4f (%s) vs s2=%.4f (%s)",
                        gap,
                        gap_threshold,
                        scored[0][0],
                        self._resolve_file_name(scored[0][2]),
                        scored[1][0],
                        self._resolve_file_name(scored[1][2]),
                    )
                primary_n = 0

        # 当候选来自 ≤2 个文件时，放宽 per_file_cap 以避免选不满 limit
        unique_files = {self._resolve_file_name(doc) for _, _, doc in scored}
        effective_per_file_cap = per_file_cap
        if len(unique_files) <= 2:
            effective_per_file_cap = max(per_file_cap, limit)

        selected: list[Document] = []
        selected_keys: set[str] = set()
        source_counter: dict[str, int] = {}
        file_counter: dict[str, int] = {}

        def try_add(doc: Document) -> bool:
            if not self._can_add_final_doc(
                doc,
                source_counter=source_counter,
                file_counter=file_counter,
                per_source_cap=per_source_cap,
                per_file_cap=effective_per_file_cap,
            ):
                return False
            selected.append(doc)
            self._register_final_doc(
                doc, source_counter=source_counter, file_counter=file_counter
            )
            return True

        primary_file = ""
        if primary_n > 0:
            primary_file = self._resolve_file_name(scored[0][2])
            search_pool = pool_docs if pool_docs else [doc for _, _, doc in scored]
            file_ranked: list[tuple[float, float, Document, str]] = []
            for doc in search_pool:
                if self._resolve_file_name(doc) != primary_file:
                    continue
                key = self._doc_key(doc)
                if key in selected_keys:
                    continue
                info = fusion_scores.get(key, {})
                s_final = float(info.get("s_final", 0.0))
                file_ranked.append((s_final, doc, key))
            file_ranked.sort(key=lambda x: x[0], reverse=True)

            # 只取 s_final 高于阈值的 chunk，避免低质片段挤占名额
            min_primary_s = float(getattr(settings, "RAG_PRIMARY_MIN_SCORE", 0.40))
            primary_picks: list[str] = []
            for s_final, doc, key in file_ranked[:primary_n]:
                if len(selected) >= limit:
                    break
                if s_final < min_primary_s:
                    continue
                if try_add(doc):
                    selected_keys.add(key)
                    md = doc.metadata or {}
                    primary_picks.append(
                        f"#{self._embedded_chunk_label(md)}(s={s_final:.3f})"
                    )

            if primary_picks:
                logger.info(
                    "[RAG] 主文件补全 file=%s 选取 %d/%d: %s",
                    primary_file,
                    len(primary_picks),
                    primary_n,
                    ", ".join(primary_picks),
                )

        for _, _, doc in scored:
            if len(selected) >= limit:
                break
            key = self._doc_key(doc)
            if key in selected_keys:
                continue
            if (
                primary_file
                and primary_n > 0
                and self._resolve_file_name(doc) == primary_file
                and self._count_file_in_docs(selected, primary_file) >= primary_n
            ):
                continue
            if try_add(doc):
                selected_keys.add(key)

        return selected

    def _doc_rerank_metrics(
        self,
        question: str,
        doc: Document,
        fusion_scores: dict[str, dict[str, float]],
    ) -> dict[str, float]:
        key = self._doc_key(doc)
        info = fusion_scores.get(key, {})
        md = doc.metadata or {}
        rrf_dist = float(info.get("raw_distance", md.get("score", 0.0) or 0.0))
        rrf_norm = float(info.get("rrf_norm", 0.0))
        s_final = float(info.get("s_final", 0.0))
        return {
            "raw_distance": rrf_dist,
            "rrf_norm": rrf_norm,
            "s_final": s_final,
            "rerank_score": s_final,
        }

    def _format_final_chunk_detail_lines(
        self,
        question: str,
        final_docs: list[Document],
        fusion_scores: dict[str, dict[str, float]],
    ) -> list[str]:
        lines: list[str] = []
        for idx, doc in enumerate(final_docs, start=1):
            md = doc.metadata or {}
            label = self._short_file_label(md)
            chunk_label = self._embedded_chunk_label(md)
            m = self._doc_rerank_metrics(question, doc, fusion_scores)
            content_preview = (doc.page_content or "").strip()[:200]
            lines.append(
                f"    {idx}. {label}#{chunk_label}\n"
                f"       rrf={m['rrf_norm']:.3f}  s_final={m['s_final']:.3f}  "
                f"text=\"{content_preview}\""
            )
        return lines

    def _log_retrieve_summary(
        self,
        *,
        question: str,
        final_docs: list[Document],
        context_len: int,
        fusion_scores: dict[str, dict[str, float]],
    ) -> None:
        """仅输出最终送入 LLM 的 top-N chunk 明细。"""
        q_display = question if len(question) <= 200 else question[:200] + "…"
        detail_lines = self._format_final_chunk_detail_lines(
            question, final_docs, fusion_scores
        )
        chunks_block = "\n".join(detail_lines) if detail_lines else "    (none)"
        logger.info(
            "[RAG] 检索完成 final=%d ctx=%d\n"
            "  q: %s\n"
            "  chunks:\n%s",
            len(final_docs),
            context_len,
            q_display,
            chunks_block,
        )

    def _log_rerank_preview(
        self,
        question: str,
        scored: list[tuple[float, float, Document]],
        fusion_scores: dict[str, dict[str, float]],
        *,
        final_limit: int,
        per_source_cap: int,
        per_file_cap: int,
        pool_docs: list[Document] | None = None,
        limit: int | None = None,
    ) -> None:
        """打印 rerank 排序后的前 N 条，并标注主文件补全 + cap 后是否进 LLM。"""
        if not scored:
            return
        log_n = limit if limit is not None else settings.RAG_RERANK_LOG_TOP_N
        if log_n <= 0:
            return

        selected = self._build_final_selection(
            question, scored, pool_docs or [], fusion_scores, final_limit
        )
        selected_keys = {self._doc_key(d) for d in selected}
        primary_n = max(0, int(settings.RAG_PRIMARY_FILE_CHUNKS))
        primary_file = (
            self._resolve_file_name(scored[0][2]) if scored and primary_n > 0 else ""
        )

        cap_tags: dict[int, str] = {}
        for idx, (_, _, doc) in enumerate(scored):
            key = self._doc_key(doc)
            if key in selected_keys:
                cap_tags[idx] = "→LLM"
            elif (
                primary_file
                and primary_n > 0
                and self._resolve_file_name(doc) == primary_file
            ):
                cap_tags[idx] = "primary_full"
            else:
                cap_tags[idx] = "over"

        lines: list[str] = []
        for rank, (rerank_score, _, doc) in enumerate(scored[:log_n], start=1):
            md = doc.metadata or {}
            label = self._short_file_label(md)
            chunk_label = self._embedded_chunk_label(md)
            tag = cap_tags.get(rank - 1, "")
            m = self._doc_rerank_metrics(question, doc, fusion_scores)
            lines.append(
                f"    {rank}. [{tag}] {label}#{chunk_label}\n"
                f"       rerank={rerank_score:.3f}  rrf={m['rrf_norm']:.3f}  s_final={m['s_final']:.3f}"
            )
        logger.info(
            "[RAG] 候选 top%d/%d (rerank, cap 前 → LLM≤%d):\n%s",
            min(log_n, len(scored)),
            len(scored),
            final_limit,
            "\n".join(lines),
        )

    @staticmethod
    def _escape_expr_value(value: str) -> str:
        return re.sub(r'(["\\])', r"\\\1", value or "")

    @staticmethod
    def _doc_key(doc: Document) -> str:
        metadata = doc.metadata or {}
        doc_id = str(metadata.get("id", "")).strip()
        if doc_id:
            return f"id:{doc_id}"
        source = str(metadata.get("source", "")).strip()
        chunk_index = str(metadata.get("chunk_index", ""))
        text_hash = str(hash(doc.page_content or ""))
        return f"src:{source}|chunk:{chunk_index}|h:{text_hash}"

    @staticmethod
    def _cosine_similarity(v1: list[float], v2: list[float]) -> float:
        if not v1 or not v2 or len(v1) != len(v2):
            return 0.0
        dot = sum(a * b for a, b in zip(v1, v2))
        n1 = math.sqrt(sum(a * a for a in v1))
        n2 = math.sqrt(sum(b * b for b in v2))
        if n1 == 0 or n2 == 0:
            return 0.0
        return dot / (n1 * n2)

    @staticmethod
    def _dedupe_tokens(tokens: list[str]) -> list[str]:
        """按出现顺序去重，过滤空白 token。"""
        out: list[str] = []
        seen: set[str] = set()
        for token in tokens:
            t = (token or "").strip()
            if not t:
                continue
            if t in seen:
                continue
            seen.add(t)
            out.append(t)
        return out

    @staticmethod
    def _is_truthy_env(value: str | None) -> bool:
        if value is None:
            return False
        return value.strip().lower() in {"1", "true", "yes", "on", "y"}

    @staticmethod
    def _parse_chunk_index_filter(value: str | None) -> set[int]:
        if not value:
            return set()
        out: set[int] = set()
        for part in value.split(","):
            p = part.strip()
            if not p:
                continue
            try:
                out.add(int(p))
            except Exception:
                continue
        return out

    @staticmethod
    def _parse_int_env(value: str | None, default: int, minimum: int = 0) -> int:
        try:
            parsed = int((value or "").strip())
        except Exception:
            parsed = default
        return max(minimum, parsed)

    @staticmethod
    def _bm25_debug_options() -> dict[str, Any]:
        """通过环境变量控制 BM25 调试日志，避免常态化刷屏。"""
        enabled = RagService._is_truthy_env(os.getenv("RAG_BM25_DEBUG"))
        file_filter = (os.getenv("RAG_BM25_DEBUG_FILE", "") or "").strip()
        chunk_filter = RagService._parse_chunk_index_filter(
            os.getenv("RAG_BM25_DEBUG_CHUNKS", "")
        )
        max_terms = RagService._parse_int_env(
            os.getenv("RAG_BM25_DEBUG_MAX_TERMS"), default=12, minimum=1
        )
        preview_len = RagService._parse_int_env(
            os.getenv("RAG_BM25_DEBUG_PREVIEW"), default=120, minimum=0
        )
        return {
            "enabled": enabled,
            "file_filter": file_filter,
            "chunk_filter": chunk_filter,
            "max_terms": max_terms,
            "preview_len": preview_len,
        }

    @staticmethod
    def _tokenize_sparse(text: str) -> list[str]:
        lower = (text or "").strip().lower()
        if not lower:
            return []
        tokens: list[str] = re.findall(r"[a-z0-9_./:-]{2,}", lower)
        chinese_groups = re.findall(r"[\u4e00-\u9fff]+", lower)
        if not chinese_groups:
            return RagService._dedupe_tokens(tokens)

        jieba_tokens: list[str] = []
        if jieba is not None:
            for grp in chinese_groups:
                try:
                    jieba_tokens.extend([w for w in jieba.lcut(grp) if len(w.strip()) >= 2])
                except Exception:
                    pass

        # 与 jieba 结果并行保留 2~4gram，避免“有词但未切出关键词”导致 sparse_raw=0。
        gram_tokens: list[str] = []
        for group in chinese_groups:
            n = len(group)
            for gram_len in range(2, min(4, n) + 1):
                for i in range(0, n - gram_len + 1):
                    gram_tokens.append(group[i : i + gram_len])

        merged_tokens = tokens + jieba_tokens + gram_tokens
        return RagService._dedupe_tokens(merged_tokens)

    def _bm25_scores(
        self, question: str, docs: list[Document],
        expanded_query: str | None = None,
    ) -> list[float]:
        if not docs:
            return []
        # BM25 查询侧使用扩展后的 query（含同义词），文档侧不变
        q_text = expanded_query or question
        q_tokens = self._tokenize_sparse(q_text)
        if not q_tokens:
            return [0.0 for _ in docs]
        doc_tfs: list[Counter[str]] = []
        doc_lens: list[int] = []
        df: Counter[str] = Counter()
        for doc in docs:
            # 优先用 child 文本（更精确），回退到 page_content（parent 文本）
            text = (doc.metadata or {}).get("best_child_text", "") or doc.page_content or ""
            tokens = self._tokenize_sparse(text)
            tf = Counter(tokens)
            doc_tfs.append(tf)
            doc_lens.append(sum(tf.values()))
            for term in tf.keys():
                df[term] += 1
        n_docs = len(docs)
        avgdl = sum(doc_lens) / max(n_docs, 1)
        k1 = max(0.1, float(settings.RAG_BM25_K1))
        b = min(max(float(settings.RAG_BM25_B), 0.0), 1.0)
        scores = [0.0 for _ in docs]
        query_terms = Counter(q_tokens)
        for term, qtf in query_terms.items():
            df_t = df.get(term, 0)
            if df_t <= 0:
                continue
            idf = math.log(1.0 + (n_docs - df_t + 0.5) / (df_t + 0.5))
            for idx, tf in enumerate(doc_tfs):
                f = float(tf.get(term, 0))
                if f <= 0:
                    continue
                dl = float(doc_lens[idx])
                denom = f + k1 * (1.0 - b + b * (dl / max(avgdl, 1e-9)))
                scores[idx] += qtf * idf * (f * (k1 + 1.0) / max(denom, 1e-9))

        debug_opts = self._bm25_debug_options()
        if debug_opts["enabled"]:
            q_terms_set = set(query_terms.keys())
            q_terms = sorted(q_terms_set)[: debug_opts["max_terms"]]
            logger.info(
                "[RAG][BM25][debug] q=%s q_terms=%s total_terms=%d docs=%d",
                question[:200],
                q_terms,
                len(query_terms),
                len(docs),
            )
            file_filter = debug_opts["file_filter"]
            chunk_filter = debug_opts["chunk_filter"]
            max_terms = debug_opts["max_terms"]
            preview_len = debug_opts["preview_len"]
            for idx, doc in enumerate(docs):
                md = doc.metadata or {}
                file_name = str(md.get("file_name", "")).strip()
                source = str(md.get("source", "")).strip()
                chunk_index_raw = md.get("chunk_index", "")
                chunk_index: int | None = None
                try:
                    chunk_index = int(chunk_index_raw)
                except Exception:
                    chunk_index = None

                file_ok = True
                if file_filter:
                    file_ok = file_filter in file_name or file_filter in source
                chunk_ok = True
                if chunk_filter:
                    chunk_ok = chunk_index is not None and chunk_index in chunk_filter
                if not (file_ok and chunk_ok):
                    continue

                doc_tf = doc_tfs[idx]
                hit_terms = sorted([t for t in q_terms_set if doc_tf.get(t, 0) > 0])[:max_terms]
                miss_terms = sorted([t for t in q_terms_set if doc_tf.get(t, 0) <= 0])[:max_terms]
                preview = (doc.page_content or "").replace("\n", " ")
                if preview_len > 0:
                    preview = preview[:preview_len]
                else:
                    preview = ""

                logger.info(
                    "[RAG][BM25][debug] file=%s chunk=%s sparse_raw=%.4f hits=%s miss=%s preview=%s",
                    file_name or Path(source).name or "unknown",
                    chunk_index_raw,
                    scores[idx],
                    hit_terms,
                    miss_terms,
                    preview,
                )
        return scores

    @staticmethod
    def _rank_by_scores(scores: list[float], reverse: bool = True) -> list[int]:
        if not scores:
            return []
        order = sorted(range(len(scores)), key=lambda i: scores[i], reverse=reverse)
        ranks = [0 for _ in scores]
        for rank, idx in enumerate(order, start=1):
            ranks[idx] = rank
        return ranks

    @staticmethod
    def _rrf_from_single_rank(ranks: list[int], k: int) -> list[float]:
        """单路 RRF：1/(k+rank)，用于稀疏/稠密排名拆分。"""
        n = len(ranks)
        if n == 0:
            return []
        safe_k = max(1, int(k))
        out: list[float] = []
        for rank in ranks:
            r = rank if rank > 0 else n + safe_k
            out.append(1.0 / (safe_k + r))
        return out

    @staticmethod
    def _rrf_from_ranks(rank_dense: list[int], rank_sparse: list[int], k: int) -> list[float]:
        n = min(len(rank_dense), len(rank_sparse))
        if n == 0:
            return []
        safe_k = max(1, int(k))
        out: list[float] = []
        for i in range(n):
            rd = rank_dense[i] if rank_dense[i] > 0 else n + safe_k
            rs = rank_sparse[i] if rank_sparse[i] > 0 else n + safe_k
            out.append(1.0 / (safe_k + rd) + 1.0 / (safe_k + rs))
        return out

    @staticmethod
    def _low_signal_penalty(text: str) -> float:
        s = (text or "").strip().lower()
        if not s:
            return 0.0
        hit = 0
        for marker in ("版权声明", "文档编号", "文档日期", "文档版本", "内部公开"):
            if marker in s:
                hit += 1
        if hit >= 3 and len(s) < 700:
            return 1.5
        if hit >= 2 and len(s) < 450:
            return 1.0
        return 0.0

    def _get_llm(self, temperature: float = 0.3):
        api_key = settings.LLM_API_KEY
        if not api_key:
            logger.warning("LLM_API_KEY 未设置，将使用空字符串")
        final_api_key = SecretStr(api_key) if api_key else None
        return ChatOpenAI(
            base_url=settings.LLM_BASE_URL,
            model=settings.LLM_MODEL,
            api_key=final_api_key,
            temperature=temperature,
            timeout=20.0,
            extra_body={
                "chat_template_kwargs": {"enable_thinking": False},
            },
        )

    @staticmethod
    def _html_details_to_markdown(text: str) -> str:
        """将 HTML <details> 标签转换为 Markdown，保留内部 mermaid 代码块。"""
        def _convert(match: re.Match) -> str:
            block = match.group(0)
            summary_match = re.search(r'<summary[^>]*>(.*?)</summary>', block, re.DOTALL | re.IGNORECASE)
            title = summary_match.group(1).strip() if summary_match else ''
            inner = re.sub(r'<summary[^>]*>.*?</summary>', '', block, flags=re.DOTALL | re.IGNORECASE)
            inner = re.sub(r'^</?details[^>]*>', '', inner, flags=re.IGNORECASE | re.MULTILINE)
            inner = inner.strip()
            if title:
                return f'\n**{title}**\n\n{inner}\n'
            return f'\n{inner}\n'

        return re.sub(
            r'<details[^>]*>.*?</details>', _convert, text, flags=re.DOTALL | re.IGNORECASE
        )

    @staticmethod
    def _html_table_to_markdown(text: str) -> str:
        """将 HTML <table> 标签转换为 Markdown 表格，使 LLM 能正确理解并输出。"""
        def _strip_tags(s: str) -> str:
            return re.sub(r'<[^>]+>', '', s).strip()

        def _convert(match: re.Match) -> str:
            table_html = match.group(0)
            rows = re.findall(r'<tr[^>]*>(.*?)</tr>', table_html, re.DOTALL | re.IGNORECASE)
            md_rows: list[str] = []
            for row_html in rows:
                cells = re.findall(
                    r'<(?:th|td)[^>]*>(.*?)</(?:th|td)>', row_html, re.DOTALL | re.IGNORECASE
                )
                if cells:
                    md_rows.append('| ' + ' | '.join(_strip_tags(c) for c in cells) + ' |')
            if len(md_rows) < 2:
                return '\n'.join(md_rows) if md_rows else table_html
            num_cols = md_rows[0].count('|') - 1
            if num_cols < 1:
                return '\n'.join(md_rows)
            sep = '|' + '|'.join([' --- '] * num_cols) + '|'
            md_rows.insert(1, sep)
            return '\n'.join(md_rows)

        return re.sub(
            r'<table[^>]*>.*?</table>', _convert, text, flags=re.DOTALL | re.IGNORECASE
        )

    def _format_docs_for_context(self, docs: list[Document]) -> str:
        chunks: list[str] = []
        for idx, doc in enumerate(docs, start=1):
            md = doc.metadata or {}
            source = str(md.get("source", "unknown"))
            file_name = str(md.get("file_name", Path(source).name if source else "unknown"))
            chunk_label = self._embedded_chunk_label(md)
            text = self._html_details_to_markdown(
                self._html_table_to_markdown(doc.page_content)
            )
            chunks.append(
                f"[片段{idx}] 文件: {file_name} | source: {source} | chunk: {chunk_label}\n{text}"
            )
        return "\n\n---\n\n".join(chunks)

    def _collect_sources(self, vector_store, selected_source: str | None) -> dict[str, str]:
        scan_limit = int(getattr(settings, "RAG_FILE_SCAN_LIMIT", 10000))
        source_to_file: dict[str, str] = {}
        if selected_source:
            normalized = self._normalize_path(selected_source)
            source_to_file[normalized] = Path(normalized).name
            logger.debug("[RAG] 指定 source，跳过扫描: %s", Path(normalized).name)
            return source_to_file
        try:
            all_docs = vector_store.get(limit=scan_limit)
            metadatas = all_docs.get("metadatas", [])
            for md in metadatas:
                source = self._normalize_path(str(md.get("source", "")).strip())
                if not source:
                    continue
                file_name = str(md.get("file_name", "")).strip() or Path(source).name
                source_to_file[source] = file_name
            logger.debug(
                "[RAG] source 扫描: unique_sources=%d, scan_limit=%d",
                len(source_to_file),
                scan_limit,
            )
        except Exception:
            logger.exception("[RAG][S_file] source 扫描失败，后续仅依赖 A 集合")
        return source_to_file

    def _score_files(
        self, question: str, vector_store, source_to_file: dict[str, str]
    ) -> tuple[dict[str, float], dict[str, float], list[tuple[str, float]], bool]:
        """返回 (applied s_file, raw cosine, ranked by raw desc, all_zero)."""
        if not source_to_file:
            return {}, {}, [], True
        unique_files = sorted(set(source_to_file.values()))
        if not unique_files:
            return {}, {}, [], True
        threshold = settings.RAG_FILE_SIM_THRESHOLD
        try:
            q_emb = vector_store.embedding_function.embed_query(question)
            f_embs = vector_store.embedding_function.embed_documents(unique_files)
            raw_scores = [self._cosine_similarity(q_emb, emb) for emb in f_embs]
            file_raw = {name: score for name, score in zip(unique_files, raw_scores)}
            max_raw = max(raw_scores) if raw_scores else 0.0
            all_zero = max_raw < threshold
            if all_zero:
                file_applied = {name: 0.0 for name in unique_files}
            else:
                file_applied = dict(file_raw)
            ranked = sorted(file_raw.items(), key=lambda x: x[1], reverse=True)
            logger.debug(
                "[RAG] s_file raw: %s | τ=%.4f | %s",
                ", ".join(f"{name}={score:.4f}" for name, score in ranked),
                threshold,
                "all_zero" if all_zero else "pass",
            )
            return file_applied, file_raw, ranked, all_zero
        except Exception:
            logger.exception("[RAG][S_file] 文件向量打分失败，降级为纯 chunk 分")
            return {}, {}, [], True

    def _cross_encoder_rerank(
        self,
        question: str,
        docs: list[Document],
    ) -> list[float]:
        """使用 LLM 对候选文档做批量相关性评分，返回每个文档的归一化分数。

        一次 LLM 调用批量处理所有候选文档，返回 0~1 之间的相关度分数列表。
        失败时返回全 1.0（不改变原有排序）。
        """
        n = len(docs)
        if n <= 1:
            return [1.0] * n

        # 截断每个文档文本以控制 prompt 长度
        passages: list[str] = []
        for i, doc in enumerate(docs):
            text = (doc.page_content or "")[:600]
            passages.append(f"[{i}] {text}\n")

        prompt = f"""对以下文档片段与用户问题的相关度打分，输出每行一个分数（0-1 之间的浮点数，1=高度相关，0=完全无关）。
严格按给出的序号顺序输出，不要输出任何解释、序号或括号。

用户问题：{question[:300]}

文档片段：
{''.join(passages)}
分数（每行一个，共{n}行）："""

        try:
            llm = self._get_llm(temperature=0.0)
            response = llm.invoke(prompt)
            raw = (response.content if hasattr(response, "content") else str(response)).strip()
            # 提取每行中的浮点数
            scores: list[float] = []
            for line in raw.split("\n"):
                match = re.search(r"(\d+\.?\d*)", line)
                if match:
                    val = float(match.group(1))
                    scores.append(min(max(val, 0.0), 1.0))
            # 补齐到 n 个
            while len(scores) < n:
                scores.append(1.0)
            scores = scores[:n]

            logger.debug(
                "[CrossEncoder] 批量评分 %d docs: %s",
                n,
                ", ".join(f"{s:.3f}" for s in scores),
            )
            return scores
        except Exception:
            logger.exception("[CrossEncoder] 评分失败，降级为原排序")
            return [1.0] * n

    def _rerank_docs(
        self,
        question: str,
        docs: list[Document],
        fusion_scores: dict[str, dict[str, float]],
        limit: int,
        pool_docs: list[Document] | None = None,
    ) -> list[Document]:
        if not docs:
            return []
        scored: list[tuple[float, float, Document]] = []
        for doc in docs:
            metrics = self._doc_rerank_metrics(question, doc, fusion_scores)
            scored.append(
                (metrics["rerank_score"], metrics["rrf_norm"], doc)
            )
        scored.sort(key=lambda x: (x[0], x[1]), reverse=True)

        per_source_cap = settings.RAG_PER_SOURCE_CAP
        per_file_cap = settings.RAG_PER_FILE_CAP
        self._log_rerank_preview(
            question,
            scored,
            fusion_scores,
            final_limit=limit,
            per_source_cap=per_source_cap,
            per_file_cap=per_file_cap,
            pool_docs=pool_docs,
            limit=len(docs),
        )
        selected = self._build_final_selection(
            question, scored, pool_docs or [], fusion_scores, limit
        )

        logger.debug(
            "[RAG] rerank %d→%d (primary_file≤%d, cap source=%d file=%d): %s",
            len(docs),
            len(selected),
            settings.RAG_PRIMARY_FILE_CHUNKS,
            per_source_cap,
            per_file_cap,
            self._format_chunk_refs(selected, fusion_scores),
        )
        return selected

    def hybrid_retrieve(
        self,
        question: str,
        group_id: str,
        selected_source: str | None = None,
        intent_filter: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        if not group_id or not group_id.strip():
            raise ValueError("group_id 不能为空")
        question = self._extract_question_text(question)
        if not question:
            return {"final_docs": [], "context": "", "fusion_rows": []}

        # 检索缓存：缓存 key = group_id + query hash
        cache_enabled = getattr(settings, "RAG_RETRIEVE_CACHE_ENABLED", False)
        cache_key = ""
        if cache_enabled:
            import hashlib
            cache_key = f"rag:cache:{group_id}:{hashlib.md5(question.encode()).hexdigest()}"
            try:
                from config.redis_conn import redis_manager
                redis_client = redis_manager.client
                cached = redis_client.get(cache_key)
                if cached:
                    logger.debug("[RAG][Cache] 命中缓存 key=%s", cache_key)
                    return {
                        "final_docs": [],
                        "context": cached,
                        "fusion_rows": [],
                        "from_cache": True,
                    }
            except Exception:
                logger.exception("[RAG][Cache] 读取缓存失败，继续正常检索")

        vector_store = store.get_vector_store(group_id=group_id)
        parent_mode = bool(settings.RAG_A_PARENT_ENABLED)
        fetch_k_global = (
            settings.RAG_A_FETCH_K_CHILD if parent_mode else settings.RAG_FETCH_K_GLOBAL
        )
        fusion_top_k = settings.RAG_FUSION_TOP_K
        final_top_k = settings.RAG_FINAL_TOP_K
        dense_limit = getattr(settings, "RAG_DENSE_RECALL_LIMIT", 150)
        sparse_limit = getattr(settings, "RAG_SPARSE_RECALL_LIMIT", 150)

        # 构建 Milvus 过滤表达式：source 过滤 + 意图分类过滤（聊天跨组检索，不限制 group_id）
        filter_parts: list[str] = []
        if selected_source:
            normalized = self._normalize_path(selected_source)
            filter_parts.append(
                f'source == "{self._escape_expr_value(normalized)}"'
            )
            logger.debug("[RAG] A 集合按 source 过滤: %s", Path(normalized).name)
        if intent_filter:
            intent_expr = self._intent_to_milvus_filter(intent_filter)
            if intent_expr:
                filter_parts.append(f"({intent_expr})")
                logger.info(
                    "[RAG] 意图路由: doc_type=%s subsystem=%s expr=%s",
                    intent_filter.get("doc_type", "general"),
                    intent_filter.get("subsystem", "general"),
                    intent_expr,
                )
        selected_expr = " && ".join(filter_parts) if filter_parts else None

        # 意图过滤无命中时，退化到全库检索
        if selected_expr:
            try:
                test_hits = vector_store.similarity_search(
                    question, k=1, expr=selected_expr, as_parent=parent_mode
                )
                if not test_hits:
                    logger.warning(
                        "[RAG] 意图过滤无命中，降级全库检索: expr=%s", selected_expr
                    )
                    # 仅保留 source 过滤（如果有），移除 intent 部分
                    if selected_source:
                        normalized = self._normalize_path(selected_source)
                        selected_expr = (
                            f'source == "{self._escape_expr_value(normalized)}"'
                        )
                    else:
                        selected_expr = None
            except Exception:
                logger.exception("[RAG] 意图过滤预检失败，降级全库检索")
                if selected_source:
                    normalized = self._normalize_path(selected_source)
                    selected_expr = (
                        f'source == "{self._escape_expr_value(normalized)}"'
                    )
                else:
                    selected_expr = None

        # Query expansion：仅用于 sparse 路
        expanded_bm25: str | None = None
        try:
            from service.rag.utils.domain_dict import expand_query as _expand
            expanded_bm25 = _expand(question)
            if expanded_bm25 and expanded_bm25 != question:
                logger.debug("[QueryExpansion] 展开: %s -> %s", question[:100], expanded_bm25[:200])
        except Exception:
            logger.exception("[QueryExpansion] 展开失败，使用原 query")

        # Dense + Sparse 双路独立召回，Milvus 内部 RRF 融合
        try:
            with rag_stage("milvus_hybrid_search"):
                docs_all = vector_store.hybrid_search(
                query=question,
                k=fetch_k_global,
                dense_limit=dense_limit,
                sparse_limit=sparse_limit,
                expr=selected_expr,
                as_parent=parent_mode,
                expanded_query=expanded_bm25,
            )
        except Exception:
            logger.exception("[RAG] hybrid_search 失败")
            docs_all = []
        logger.info(
            "[RAG] hybrid召回 %s=%d (dense=%d sparse=%d k上限=%d%s)",
            "parent" if parent_mode else "chunk",
            len(docs_all),
            dense_limit,
            sparse_limit,
            fetch_k_global,
            f", expr={selected_expr!r}" if selected_expr else "",
        )

        if not docs_all:
            logger.warning("[RAG] 无候选 chunk，返回空 context")
            return {"final_docs": [], "context": "", "fusion_rows": []}

        # 硬过滤：移除文档元数据/扉页 chunk（修订记录、文件编号表等）
        before_filter = len(docs_all)
        docs_all = [d for d in docs_all if not self._is_metadata_chunk(d.page_content or "")]
        filtered_count = before_filter - len(docs_all)
        if filtered_count:
            logger.info("[RAG] 元数据硬过滤 %d/%d chunks", filtered_count, before_filter)
        if not docs_all:
            logger.warning("[RAG] 过滤后无候选 chunk")
            return {"final_docs": [], "context": "", "fusion_rows": []}

        # 构建 source → file_name 映射
        source_to_file: dict[str, str] = {}
        for d in docs_all:
            source = self._normalize_path(str((d.metadata or {}).get("source", "")).strip())
            if not source or source in source_to_file:
                continue
            source_to_file[source] = str((d.metadata or {}).get("file_name", "")).strip() or Path(source).name

        # 分数级融合：Milvus RRF 分 (child 粒度, 已含 dense+sparse 双路信号) → z-score sigmoid → 降权/加权
        fusion_scores: dict[str, dict[str, float]] = {}
        fusion_rows: list[dict[str, Any]] = []

        # 1) 收集 Milvus RRF distance (lower=better, 已融合 dense+sparse)
        rrf_raw: list[float] = []
        for doc in docs_all:
            rrf_dist = float((doc.metadata or {}).get("score", 0.0) or 0.0)
            rrf_raw.append(rrf_dist)

        # 2) z-score sigmoid 归一化 (RRF distance 越小越好, sigmoid 自动反转)
        sigmoid_scale = float(getattr(settings, "RAG_SIGMOID_SCALE", 1.5))

        def _z_sigmoid(scores: list[float], scale: float) -> list[float]:
            n = len(scores)
            m = sum(scores) / max(n, 1)
            var = sum((s - m) ** 2 for s in scores) / max(n, 1)
            sd = math.sqrt(var)
            if sd < 1e-9:
                return [0.5] * n
            return [1.0 / (1.0 + math.exp(-(s - m) / sd * scale)) for s in scores]

        rrf_norm = _z_sigmoid(rrf_raw, sigmoid_scale)

        for idx, doc in enumerate(docs_all):
            md = doc.metadata or {}
            rrf_dist = rrf_raw[idx]
            source = self._normalize_path(str(md.get("source", "")).strip())
            file_name = str(md.get("file_name", "")).strip() or source_to_file.get(
                source, Path(source).name if source else "unknown"
            )

            s_final = rrf_norm[idx]
            # 文档元数据降权（修订记录/编写目的/版权声明等无关片段）
            penalty = self._boilerplate_penalty(doc.page_content or "")
            s_final *= penalty
            # 文档名与 query 词面重叠：相关文档加分，跨域文档降权
            s_final *= self._doc_name_relevance(question, doc)
            key = self._doc_key(doc)
            fusion_scores[key] = {
                "raw_distance": rrf_dist,
                "rrf_norm": rrf_norm[idx],
                "s_final": s_final,
            }
            fusion_rows.append(
                {
                    "key": key,
                    "doc": doc,
                    "source": source,
                    "file_name": file_name,
                    "chunk_index": md.get("chunk_index", ""),
                    "embedded_chunk": self._embedded_chunk_label(md),
                    "rrf_dist": rrf_dist,
                    "s_final": s_final,
                }
            )

        fusion_rows.sort(key=lambda x: x["s_final"], reverse=True)
        top_fusion_rows = fusion_rows[:fusion_top_k]
        docs_for_rerank = [row["doc"] for row in top_fusion_rows]

        fusion_preview = ", ".join(
            f"{r['file_name']}#{r['embedded_chunk']}(rrf={r['rrf_dist']:.4f},s={r['s_final']:.3f})"
            for r in top_fusion_rows[:5]
        )
        logger.info(
            "[RAG] RRF z-sigmoid scale=%.1f %d→%d top5: %s",
            sigmoid_scale, len(fusion_rows), len(docs_for_rerank),
            fusion_preview or "-",
        )

        # Cross-encoder 精排（可配置开关）
        cross_encoder_scores: list[float] | None = None
        if getattr(settings, "RAG_CROSS_ENCODER_ENABLED", False) and len(docs_for_rerank) > 1:
            pre_filter_n = min(
                settings.RAG_CROSS_ENCODER_PRE_FILTER, len(docs_for_rerank)
            )
            # 先用公式重排序做预筛选，再送 cross-encoder
            pre_ranked = self._rerank_docs(
                question=question,
                docs=docs_for_rerank,
                fusion_scores=fusion_scores,
                limit=pre_filter_n,
                pool_docs=docs_all,
            )
            try:
                with rag_stage("cross_encoder"):
                    cross_encoder_scores = self._cross_encoder_rerank(question, pre_ranked)
                # 将 cross-encoder 分数合并回 fusion_scores
                ce_weight = settings.RAG_CROSS_ENCODER_WEIGHT
                for idx, doc in enumerate(pre_ranked):
                    key = self._doc_key(doc)
                    ce_score = cross_encoder_scores[idx] if idx < len(cross_encoder_scores) else 1.0
                    if key in fusion_scores:
                        old_s = fusion_scores[key].get("s_final", 0.0)
                        fusion_scores[key]["ce_score"] = ce_score
                        fusion_scores[key]["s_final"] = (
                            old_s * (1.0 - ce_weight) + ce_score * ce_weight
                        )
                logger.info(
                    "[CrossEncoder] 精排 %d docs → ce_weight=%.2f",
                    len(pre_ranked),
                    ce_weight,
                )
            except Exception:
                logger.exception("[CrossEncoder] 精排失败，降级为公式排序")

        with rag_stage("rerank"):
            final_docs = self._rerank_docs(
                question=question,
                docs=docs_for_rerank,
                fusion_scores=fusion_scores,
                limit=final_top_k,
                pool_docs=docs_all,
            )

        with rag_stage("format_context"):
            context = self._format_docs_for_context(final_docs)
        self._log_retrieve_summary(
            question=question,
            final_docs=final_docs,
            context_len=len(context),
            fusion_scores=fusion_scores,
        )

        # 写入缓存
        if cache_enabled and cache_key and context:
            try:
                from config.redis_conn import redis_manager
                redis_client = redis_manager.client
                ttl = getattr(settings, "RAG_RETRIEVE_CACHE_TTL", 3600)
                redis_client.setex(cache_key, ttl, context)
                logger.debug("[RAG][Cache] 写入缓存 key=%s ttl=%d", cache_key, ttl)
            except Exception:
                logger.exception("[RAG][Cache] 写入缓存失败")

        return {"final_docs": final_docs, "context": context, "fusion_rows": top_fusion_rows}

    def get_chain(
        self,
        group_id: str,
        filter_expr: str | None = None,
        selected_source: str | None = None,
    ) -> Runnable:
        if not group_id or not group_id.strip():
            raise ValueError("group_id 不能为空")
        self._build_chain(
            group_id=group_id,
            filter_expr=filter_expr,
            selected_source=selected_source,
        )
        if self._chain is None:
            raise RuntimeError("Chain 未初始化，请调用 _build_chain()")
        return self._chain

    def _build_chain(
        self,
        group_id: str | None = None,
        filter_expr: str | None = None,
        selected_source: str | None = None,
    ):
        if not group_id or not group_id.strip():
            raise ValueError("group_id 不能为空")
        prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    """你是一个严格的结构化问答助手。即使处于流式输出模式，也必须遵守以下规则。请仅根据下面「参考内容」回答问题，若没有足够信息则如实说明。

【硬性输出规则 — 逐条严格遵守】

第一部分：结构要求
1. 必须以 ## 标题直接开头，禁止输出任何前置文字（禁止「好的」「根据参考内容」「以下是回答」等引导语）。
2. 必须先输出完整的小标题（## 或 ###），然后换行，再输出该小节内容。禁止边写标题边写内容。
3. 每个小节内容控制在 3-5 句话或一个列表，严禁输出一大段超过 100 字的连续纯文本。
4. 小节之间必须有空行（即输出一个空行）分隔。

第二部分：内容格式
5. 步骤类内容必须使用 1. 2. 3. 编号列表，每条单独一行。
6. 并列要点必须使用 - 无序列表，每条单独一行。
7. 每段文字控制在 2-3 句以内，超过必须分段或改用列表。
8. 如果参考内容中包含流程/步骤类信息（如"XX 分钟内通报""随后进行 XX""完成后通知 XX"等含先后顺序和数量约束的描述），必须将流程用 ```mermaid 围栏代码块绘制流程图（graph LR 方向），每个步骤作为一个节点，流程节点的标签中必须严格保留参考内容中的具体数值（时间、数量、频率、百分比等）。例如参考内容写"2 分钟快速通报"，节点必须标注 `"2分钟快速通报"` 而不是 `"快速通报"`。流程图前后各空一行，禁止编造或简化节点文字。
9. 对于结构化数据（参数对比、分类说明、流程步骤、数值列表、配置参数等），必须优先使用 Markdown 表格呈现。参考内容中已有的表格必须原样保留，表格前后各空一行。使用表格时确保列对齐，表头加粗自动识别。

第三部分：格式约束
10. 严格使用 Markdown 格式，正确使用 ##、###、**粗体**（仅用于关键术语）、列表等。
11. 数字、编号、设备编号等必须用反引号包裹（如 `RZKJ-LF-001`）。
12. 每输出完一个语义完整的段落（一个标题+内容、或一个完整列表），必须立即换行。

第四部分：禁止事项
13. 禁止输出连续超过 5 行纯文本（不含标题/列表标记的文本）。
14. 禁止整段加粗，**粗体**仅用于单个关键术语（如设备名、风险等级）。
15. 禁止编造内容，只基于参考内容回答。
16. 禁止输出思考过程、think 标签或任何非回答内容。
17. 若参考内容为"知识库存在文档但未匹配"，直接友好礼貌地回复：「未找到与您问题相关的文档内容，请尝试换个方式描述问题或上传相关文档。」不输出任何其他内容。
18. 若参考内容为空，直接友好礼貌地回复：「请先上传文档，我才能基于文档内容为您解答。」不输出任何其他内容。

参考内容：
{context}""",
                ),
                ("placeholder", "{chat_history}"),
                ("human", "{question}"),
            ]
        )
        llm = self._get_llm()

        def format_input(x):
            raw = x.get("question", x) if isinstance(x, dict) else x
            q = self._extract_question_text(raw)
            session_id = x.get("session_id", "") if isinstance(x, dict) else ""

            # 简单问候/闲聊：跳过 RAG 检索，直接让 LLM 自由回答
            if self._is_greeting_or_chitchat(q):
                logger.info("[RAG] 检测到问候/闲聊，跳过检索: q=%s", q)
                return {"context": "（用户打招呼或闲聊，请友好自然地回复，介绍自己并引导用户提出具体问题）", "question": q}

            # Query rewrite: 用对话历史对追问做指代消解
            search_query = q
            rewrite_enabled = getattr(settings, "RAG_QUERY_REWRITE_ENABLED", True)
            if rewrite_enabled and session_id and self._needs_rewrite(q):
                try:
                    with rag_stage("query_rewrite"):
                        from service.rag.conversation.chat_history import get_session_history as _get_hist
                        hist = _get_hist(session_id)
                        rewrite_llm = self._get_llm(temperature=0.0)
                        search_query = self._rewrite_query(q, hist.messages, llm=rewrite_llm)
                except Exception:
                    logger.exception("[QueryRewrite] 获取历史失败，使用原问题检索")

            # 意图分类：分析 query 属于哪类操作文档和子系统
            intent_filter = None
            intent_enabled = getattr(settings, "RAG_INTENT_CLASSIFY_ENABLED", True)
            if intent_enabled:
                try:
                    with rag_stage("intent_classify"):
                        intent_filter = self._classify_intent(search_query)
                except Exception:
                    logger.exception("[IntentClassify] 分类失败，使用全库检索")

            try:
                with rag_stage("hybrid_retrieve"):
                    retrieve_result = self.hybrid_retrieve(
                        question=search_query,
                        group_id=group_id,
                        selected_source=selected_source,
                        intent_filter=intent_filter,
                    )
                context = retrieve_result.get("context", "")
                fusion_rows = retrieve_result.get("fusion_rows", [])
                # 仅在检索未命中（且非缓存命中）时检查知识库是否为空
                if not fusion_rows and not context:
                    logger.info("[RAG] 未检索到任何文档")
                    # 区分"知识库为空"和"有文档但未匹配"
                    kb_has_docs = False
                    try:
                        vector_store_check = store.get_vector_store(group_id=group_id)
                        result = vector_store_check.client.query(
                            collection_name=vector_store_check.collection_name,
                            filter="",
                            limit=1,
                            output_fields=["id"],
                        )
                        kb_has_docs = len(result) > 0
                    except Exception:
                        logger.exception("[RAG] 检查知识库是否为空失败")
                    if kb_has_docs:
                        context = "知识库存在文档但未匹配"
                    else:
                        context = ""
            except Exception:
                logger.exception("[RAG][CHAIN] hybrid_retrieve 执行失败，使用空 context")
                context = ""
            return {"context": context, "question": q}

        base_chain = RunnableLambda(format_input) | prompt | llm | StrOutputParser()
        self._chain = RunnableWithMessageHistory(
            base_chain,
            get_session_history,
            input_messages_key="question",
            history_messages_key="chat_history",
        )

    def answer(self, question: str, group_id: str, filter_expr: str | None = None) -> str:
        if not group_id or not group_id.strip():
            raise ValueError("group_id 不能为空")
        chain = self.get_chain(group_id=group_id)
        result = chain.invoke(question.strip())
        return str(result) if result else "无回复"

    def answer_with_session(self, question: str, session_id: str, group_id: str) -> str:
        if not group_id or not group_id.strip():
            raise ValueError("group_id 不能为空")
        chain = self.get_chain(group_id=group_id)
        result = chain.invoke(
            {"question": question.strip(), "session_id": session_id},
            config={"configurable": {"session_id": session_id}},
        )
        return str(result) if result else "无回复"

    async def async_answer_without_rag(
        self, question: str, session_id: str
    ) -> AsyncIterator[str]:
        llm = self._get_llm()
        prompt = ChatPromptTemplate.from_messages(
            [
                ("system", """你是一个严格的结构化问答助手，服务于润泽科技数据中心运维团队。即使处于流式输出模式，也必须遵守以下规则：

1. 必须以 ## 标题直接开头，禁止输出任何前置引导语。
2. 必须先输出完整标题再输出内容，小节之间用空行分隔。
3. 每小节控制在 3-5 句话，严禁输出一大段连续纯文本。
4. 步骤类内容用 1. 2. 3. 编号列表，并列要点用 - 无序列表。
5. 严格使用 Markdown 格式，编号和设备名用反引号包裹。
6. 禁止整段加粗，禁止输出超过 5 行连续纯文本。
7. 每输出完一个语义完整的段落必须立即换行。"""),
                ("placeholder", "{chat_history}"),
                ("human", "{input}"),
            ]
        )
        base_chain = prompt | llm | StrOutputParser()
        chain_with_history = RunnableWithMessageHistory(base_chain, get_session_history)
        try:
            async for chunk in chain_with_history.astream(
                {"input": question.strip()},
                config={"configurable": {"session_id": session_id}},
            ):
                content = chunk.content if hasattr(chunk, "content") else str(chunk)
                if isinstance(content, str):
                    yield content
        except Exception as e:
            logger.error("[普通对话] 异步流式生成失败: %s", str(e))
            yield f"\n[错误] {str(e)}"

    def add_file(self, file_path: str, group_id: str, open_id: str = "") -> int:
        logger.info("[RagService] 开始添加文件到向量库: path=%s, group_id=%s, open_id=%s", file_path, group_id, open_id)
        if not group_id or not group_id.strip():
            raise ValueError("group_id 不能为空")
        count = store.add_file(file_path, group_id=group_id, open_id=open_id)
        logger.info("[RagService] 文件添加完成: chunk数量=%d", count)
        return count

    def delete_file(self, file_path: str, group_id: str) -> tuple:
        if not group_id or not group_id.strip():
            raise ValueError("group_id 不能为空")
        logger.info("[RagService] 开始删除文件及向量: path=%s, group_id=%s", file_path, group_id)
        try:
            result = store.delete_file_and_vectors(file_path, group_id=group_id)
            logger.info(
                "[RagService] 删除完成: path=%s, group_id=%s, vectors_deleted=%d, file_deleted=%s",
                file_path,
                group_id,
                result[0],
                result[1],
            )
            return result
        except Exception as e:
            logger.error("[RagService] 删除文件及向量失败: path=%s, group_id=%s, error=%s", file_path, group_id, str(e))
            raise

    def export_chunks(self, file_path: str, output_dir: str | None = None) -> int:
        if output_dir is None:
            output_dir = str(_project_root / "embedded")
        logger.info("[RagService] 开始导出切分结果: path=%s, output_dir=%s", file_path, output_dir)
        try:
            count = store.export_chunks_to_folder(file_path, output_dir)
            logger.info("[RagService] 导出切分结果完成: path=%s, chunks=%d", file_path, count)
            return count
        except Exception as e:
            logger.error("[RagService] 导出切分结果失败: path=%s, output_dir=%s, error=%s", file_path, output_dir, str(e))
            raise

    def get_vector_store_info(self, group_id: str, expr: str | None = None, open_id: str = "") -> dict:
        if not group_id or not group_id.strip():
            raise ValueError("group_id 不能为空")
        try:
            vector_store = store.get_vector_store(group_id=group_id)
            escaped_group = self._escape_expr_value(group_id)
            group_filter = f'group_id == "{escaped_group}"'
            combined = f"{group_filter} && ({expr})" if expr else group_filter
            all_docs = vector_store.get(expr=combined)
            sources = all_docs.get("metadatas", [])
            source_counts: dict[str, dict] = {}
            for source in sources:
                source_path = str(source.get("source", "unknown"))
                if source_path not in source_counts:
                    file_size = 0
                    try:
                        file_size = Path(source_path).stat().st_size
                    except OSError:
                        pass
                    source_counts[source_path] = {"chunks": 0, "size": file_size}
                source_counts[source_path]["chunks"] += 1
            return {"total_chunks": len(sources), "sources": source_counts}
        except Exception as e:
            logger.error("[RAG] 获取向量库信息失败: %s", str(e))
            return {"error": str(e)}


rag_service = RagService(load_chain=True)
