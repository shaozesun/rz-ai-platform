"""
文档切分器：将文档切分成更稳定的语义块用于向量检索。
"""
import hashlib
import re

from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter

from config.settings import settings
from repository.ingestion.loader import load_documents


DEFAULT_SEPARATORS = [
    "\n\n",
    "\n\r\n",
    "\r\n\r\n",
    "\n",
    "。\n",
    "？\n",
    "！\n",
    "；\n",
    "。",
    "？",
    "！",
    "；",
    "：",
    ".\n",
    "?\n",
    "!\n",
    ". ",
    "? ",
    "! ",
    "",
]

# MinerU/Markdown 文档专用：优先在标题/段落边界切分，表格行边界次之
MARKDOWN_SEPARATORS = [
    "\n## ",
    "\n### ",
    "\n#### ",
    "\n# ",
    "\n\n",
    "\n\r\n",
    "\r\n\r\n",
    r"\n\|",          # 表格行边界（避免表格中间被切碎）
    "\n",
    "。\n",
    "？\n",
    "！\n",
    "；\n",
    "。",
    "？",
    "！",
    "；",
    "：",
    ".\n",
    "?\n",
    "!\n",
    ". ",
    "? ",
    "! ",
    "",
]


def _doc_is_markdown(documents: list[Document]) -> bool:
    """判断文档是否为 MinerU/Markdown 产物。"""
    if not documents:
        return False
    parser = str((documents[0].metadata or {}).get("parser", ""))
    return parser.startswith("mineru")


# ==================== 表格感知切分 ====================

_TABLE_SEP_RE = re.compile(r"^\|[\s\-:|]+\|\s*$")


def _is_table_line(line: str) -> bool:
    s = line.strip()
    return s.startswith("|") and s.endswith("|") and len(s) >= 3


def _extract_table_blocks(text: str) -> list[tuple[str, bool]]:
    """将文本分割为 [(段文本, 是否表格)] 列表，保留原始顺序。

    表格块判定：以 | 开头的行紧跟 | --- | 分隔线，后续连续 | 行。
    """
    segments: list[tuple[str, bool]] = []
    lines = text.split("\n")
    i = 0
    n = len(lines)
    while i < n:
        if (
            _is_table_line(lines[i])
            and i + 1 < n
            and _TABLE_SEP_RE.match(lines[i + 1].strip())
        ):
            j = i + 2
            while j < n and _is_table_line(lines[j]):
                j += 1
            segments.append(("\n".join(lines[i:j]), True))
            i = j
        else:
            j = i
            while j < n:
                if (
                    _is_table_line(lines[j])
                    and j + 1 < n
                    and _TABLE_SEP_RE.match(lines[j + 1].strip())
                ):
                    break
                j += 1
            block = "\n".join(lines[i:j]).strip()
            if block:
                segments.append((block, False))
            i = j
    return segments


def _split_markdown_table_block(table_text: str, chunk_size: int) -> list[str]:
    """将大 markdown 表格按第一列分类值分组切分，每个子块复制表头。

    - 小表格（<=chunk_size）：整体返回
    - 大表格：按第一列分类值分组，相同分类的连续行放一起
    - 超过 chunk_size 的组：按行切，每块带表头
    """
    lines = [l for l in table_text.strip().split("\n") if l.strip()]
    if len(lines) < 3:
        return [table_text]

    header_lines = lines[:2]  # 表头行 + 分隔线
    data_lines = lines[2:]

    full = "\n".join(header_lines + data_lines)
    if len(full) <= chunk_size:
        return [full]

    # 按第一列分类值分组（空第一列或与上组相同 → 归入上一组，处理 rowspan 续行）
    groups: list[tuple[str, list[str]]] = []
    for line in data_lines:
        parts = line.split("|")
        first_col = parts[1].strip() if len(parts) > 1 else ""
        if groups and (not first_col or first_col == groups[-1][0]):
            groups[-1][1].append(line)
        else:
            groups.append((first_col, [line]))

    header_text = "\n".join(header_lines)
    header_len = len(header_text)
    chunks: list[str] = []

    for _, group_lines in groups:
        group_text = "\n".join(header_lines + group_lines)
        if len(group_text) <= chunk_size:
            chunks.append(group_text)
            continue
        # 组内仍超大：按行切，每块带表头
        batch: list[str] = []
        batch_len = header_len
        for line in group_lines:
            if batch and batch_len + 1 + len(line) > chunk_size:
                chunks.append("\n".join(header_lines + batch))
                batch = [line]
                batch_len = header_len + 1 + len(line)
            else:
                batch.append(line)
                batch_len += 1 + len(line)
        if batch:
            chunks.append("\n".join(header_lines + batch))

    return chunks


# ==================== 步骤块感知切分 ====================

_STEP_LINE_RE = re.compile(r"^\d+(?:\.\d+){1,}\s")
# 连续编号行达到此数量才视为步骤块（短编号列表如 4.1/5.1 走普通切分）
_MIN_STEP_LINES = 5


def _is_step_line(line: str) -> bool:
    return bool(_STEP_LINE_RE.match(line.strip()))


def _extract_step_blocks(text: str) -> list[tuple[str, bool]]:
    """将文本分为 [(段文本, 是否步骤块)]，保留顺序。

    步骤块：以编号行（如 9.3.1.1）开头，后续续行（非空、非新 section 标题）
    一并归入，直至空行或"第N部分"标题结束。仅当编号行数 >= _MIN_STEP_LINES
    时才标记为步骤块，避免短编号列表被单独切出丢失上下文。
    """
    lines = text.split("\n")
    segments: list[tuple[str, bool]] = []
    i = 0
    n = len(lines)
    while i < n:
        if _is_step_line(lines[i]):
            j = i + 1
            while j < n:
                if _is_step_line(lines[j]):
                    j += 1
                elif lines[j].strip() == "":
                    break
                elif re.match(r"^第\d+部分", lines[j].strip()):
                    break
                else:
                    j += 1  # 续行归入步骤块
            block = "\n".join(lines[i:j])
            step_count = sum(1 for k in range(i, j) if _is_step_line(lines[k]))
            segments.append((block, step_count >= _MIN_STEP_LINES))
            i = j
        else:
            j = i
            while j < n and not _is_step_line(lines[j]):
                j += 1
            block = "\n".join(lines[i:j])
            if block.strip():
                segments.append((block, False))
            i = j
    return segments


def _split_step_block(text: str, chunk_size: int) -> list[str]:
    """按步骤行分组切分，每块含完整步骤（含续行），不切断单步，不 overlap。"""
    lines = text.split("\n")
    groups: list[list[str]] = []
    for line in lines:
        if _is_step_line(line):
            groups.append([line])
        elif groups:
            groups[-1].append(line)
        else:
            groups.append([line])
    chunks: list[str] = []
    batch: list[str] = []
    batch_len = 0
    for grp in groups:
        grp_text = "\n".join(grp)
        grp_len = len(grp_text)
        if batch and batch_len + 1 + grp_len > chunk_size:
            chunks.append("\n".join(batch))
            batch = grp
            batch_len = grp_len
        else:
            if batch:
                batch_len += 1
            batch.extend(grp)
            batch_len += grp_len
    if batch:
        chunks.append("\n".join(batch))
    return chunks


# ==================== 小标题感知贪心打包 ====================

# 层级1边界：第N部分 / markdown 标题（最高优先级，不在中间切）
_HEADING_BOUNDARY_RE = re.compile(r"(?=^第\d+部分)|(?=^\s*#{1,6}\s)", re.MULTILINE)


def _split_by_headings(text: str) -> list[str]:
    """按 第N部分 / markdown 标题切分，每段含标题行及后续内容直至下一标题。

    无标题的文本返回整体作为一个段。保留原始顺序。
    """
    if not text or not text.strip():
        return []
    parts = _HEADING_BOUNDARY_RE.split(text)
    return [p.strip() for p in parts if p.strip()]


def _pack_structured(
    text: str,
    splitter: "RecursiveCharacterTextSplitter",
    chunk_size: int,
) -> list[str]:
    """层级结构感知贪心打包。

    层级：第N部分/markdown标题（最高优先）→ 表格/步骤/段落（大块降级）→ 句子。
    小块贪心合并到 chunk_size（不切碎段落/小节），大块（超 chunk_size）才降级
    走 _table_aware_split_text（表格/步骤/文本感知）细切，不切断原子单元。
    """
    blocks = _split_by_headings(text)
    chunks: list[str] = []
    current = ""
    for block in blocks:
        block = block.strip()
        if not block:
            continue
        if len(block) <= chunk_size:
            # 小块：贪心合并到当前 chunk
            if current and len(current) + 2 + len(block) <= chunk_size:
                current = current + "\n\n" + block
            else:
                if current:
                    chunks.append(current)
                current = block
        else:
            # 大块：先 flush 当前，再降级细切
            if current:
                chunks.append(current)
                current = ""
            chunks.extend(_table_aware_split_text(block, splitter, chunk_size))
    if current:
        chunks.append(current)
    return chunks


def _table_aware_split_documents(
    doc: Document,
    splitter: "RecursiveCharacterTextSplitter",
    chunk_size: int,
) -> list[Document]:
    """对单个文档做结构感知切分。

    最外层按 第N部分/markdown 标题贪心打包到 chunk_size（小节合并、不切碎段落），
    超出 chunk_size 的大节降级走 _table_aware_split_text（表格/步骤/文本感知）。
    保留原始顺序。
    """
    base_meta = dict(doc.metadata or {})
    chunks = _pack_structured(doc.page_content or "", splitter, chunk_size)
    return [
        Document(page_content=c, metadata=dict(base_meta))
        for c in chunks
        if c.strip()
    ]


def _table_aware_split_text(
    text: str,
    splitter: "RecursiveCharacterTextSplitter",
    chunk_size: int,
) -> list[str]:
    """对纯文本做表格感知切分，返回切分后的文本列表。"""
    segments = _extract_table_blocks(text)
    result: list[str] = []
    text_parts: list[str] = []
    for seg_text, is_table in segments:
        if is_table:
            if text_parts:
                result.extend(splitter.split_text("\n\n".join(text_parts)))
                text_parts = []
            result.extend(_split_markdown_table_block(seg_text, chunk_size))
        else:
            for sub_text, is_step in _extract_step_blocks(seg_text):
                if is_step:
                    if text_parts:
                        result.extend(splitter.split_text("\n\n".join(text_parts)))
                        text_parts = []
                    result.extend(_split_step_block(sub_text, chunk_size))
                else:
                    text_parts.append(sub_text)
    if text_parts:
        result.extend(splitter.split_text("\n\n".join(text_parts)))
    return result


def get_splitter(
    chunk_size: int,
    chunk_overlap: int,
    *,
    is_markdown: bool = False,
) -> RecursiveCharacterTextSplitter:
    return RecursiveCharacterTextSplitter(
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        length_function=len,
        add_start_index=True,
        separators=MARKDOWN_SEPARATORS if is_markdown else DEFAULT_SEPARATORS,
    )


def _same_doc_scope(a: Document, b: Document) -> bool:
    am = a.metadata or {}
    bm = b.metadata or {}
    return (
        str(am.get("source", "")) == str(bm.get("source", ""))
        and str(am.get("sheet", "")) == str(bm.get("sheet", ""))
    )


def merge_small_chunks(
    chunks: list[Document],
    min_chunk_chars: int,
    soft_max_chars: int,
) -> list[Document]:
    if not chunks:
        return []

    merged: list[Document] = []
    for chunk in chunks:
        text = (chunk.page_content or "").strip()
        if not text:
            continue
        current = Document(page_content=text, metadata=dict(chunk.metadata or {}))
        if not merged:
            merged.append(current)
            continue

        prev = merged[-1]
        prev_len = len((prev.page_content or "").strip())
        curr_len = len(text)
        can_merge = _same_doc_scope(prev, current) and (
            prev_len < min_chunk_chars
            or curr_len < min_chunk_chars
            or (prev_len < int(min_chunk_chars * 1.2) and curr_len < int(min_chunk_chars * 1.2))
        )
        if can_merge and (prev_len + 1 + curr_len) <= soft_max_chars:
            prev.page_content = f"{prev.page_content.rstrip()}\n{text}"
            continue

        merged.append(current)

    if len(merged) >= 2:
        last = merged[-1]
        prev = merged[-2]
        if (
            _same_doc_scope(prev, last)
            and len((last.page_content or "").strip()) < min_chunk_chars
        ):
            prev.page_content = f"{prev.page_content.rstrip()}\n{last.page_content.strip()}"
            merged.pop()

    return merged


def split_documents_smart(
    documents: list[Document],
    *,
    chunk_size: int = 700,
    chunk_overlap: int = 80,
    min_chunk_chars: int = 120,
) -> list[Document]:
    text_splitter = get_splitter(
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        is_markdown=_doc_is_markdown(documents),
    )

    # 表格感知切分：表格块走 _split_markdown_table_block（保留表头 + 分类分组），
    # 普通文本走原有 splitter
    all_chunks: list[Document] = []
    for doc in documents:
        all_chunks.extend(_table_aware_split_documents(doc, text_splitter, chunk_size))

    return merge_small_chunks(
        all_chunks,
        min_chunk_chars=min_chunk_chars,
        soft_max_chars=max(int(chunk_size * 1.4), chunk_size + chunk_overlap),
    )


def split_documents_with_settings(documents: list[Document]) -> list[Document]:
    return split_documents_smart(
        documents,
        chunk_size=settings.CHUNK_SIZE,
        chunk_overlap=settings.CHUNK_OVERLAP,
        min_chunk_chars=max(180, settings.CHUNK_SIZE // 4),
    )


def split_documents_parent_child(
    documents: list[Document],
    *,
    parent_chunk_size: int,
    parent_chunk_overlap: int,
    child_chunk_size: int,
    child_chunk_overlap: int,
    min_child_chars: int,
) -> tuple[list[Document], list[Document]]:
    if not documents:
        return [], []

    doc_is_md = _doc_is_markdown(documents)
    parent_splitter = get_splitter(parent_chunk_size, parent_chunk_overlap, is_markdown=doc_is_md)
    child_splitter = get_splitter(child_chunk_size, child_chunk_overlap, is_markdown=doc_is_md)
    parents: list[Document] = []
    children: list[Document] = []
    parent_global_idx = 0

    for doc in documents:
        base_meta = dict(doc.metadata or {})
        source = str(base_meta.get("source", ""))
        scope = f"{source}|{base_meta.get('sheet', '')}"

        # 表格感知父切分：表格块走 _split_markdown_table_block，普通文本走原 splitter
        raw_parents = _table_aware_split_documents(doc, parent_splitter, parent_chunk_size)
        parent_docs = merge_small_chunks(
            raw_parents,
            min_chunk_chars=max(180, parent_chunk_size // 4),
            soft_max_chars=max(
                int(parent_chunk_size * 1.4), parent_chunk_size + parent_chunk_overlap
            ),
        )
        if not parent_docs:
            continue

        child_global_idx = 0
        for p_idx, p_doc in enumerate(parent_docs):
            parent_text = (p_doc.page_content or "").strip()
            if not parent_text:
                continue
            digest = hashlib.sha1(parent_text.encode("utf-8")).hexdigest()[:16]
            parent_id = f"{scope}|p{p_idx}|{digest}"
            parent_meta = {
                **base_meta,
                "parent_id": parent_id,
                "parent_chunk_index": p_idx,
                "parent_global_index": parent_global_idx,
                "chunk_index": p_idx,
            }
            parents.append(Document(page_content=parent_text, metadata=parent_meta))

            # 表格感知子切分：含表格的父块走 _split_markdown_table_block
            child_texts = [
                t.strip()
                for t in _table_aware_split_text(parent_text, child_splitter, child_chunk_size)
                if t.strip()
            ] or [parent_text]
            accepted = [t for t in child_texts if len(t) >= min_child_chars]
            if not accepted:
                accepted = [max(child_texts, key=len)]

            for c_local_idx, child_text in enumerate(accepted):
                child_meta = {
                    **base_meta,
                    "parent_id": parent_id,
                    "parent_chunk_index": p_idx,
                    "parent_global_index": parent_global_idx,
                    "child_chunk_index": c_local_idx,
                    "chunk_index": child_global_idx,
                    "parent_text": parent_text,
                }
                children.append(Document(page_content=child_text, metadata=child_meta))
                child_global_idx += 1

            parent_global_idx += 1

    return parents, children


def split_documents_parent_child_with_settings(
    documents: list[Document],
) -> tuple[list[Document], list[Document]]:
    return split_documents_parent_child(
        documents,
        parent_chunk_size=settings.RAG_A_PARENT_CHUNK_SIZE,
        parent_chunk_overlap=settings.RAG_A_PARENT_CHUNK_OVERLAP,
        child_chunk_size=settings.RAG_A_CHILD_CHUNK_SIZE,
        child_chunk_overlap=settings.RAG_A_CHILD_CHUNK_OVERLAP,
        min_child_chars=max(80, settings.RAG_A_CHILD_CHUNK_SIZE // 5),
    )


def load_and_split_for_index(file_path: str) -> list[Document]:
    docs = load_documents(file_path)
    if not docs:
        return []
    if settings.RAG_A_PARENT_ENABLED:
        _, children = split_documents_parent_child_with_settings(docs)
        return children
    return split_documents_with_settings(docs)


def load_and_split_for_export(file_path: str) -> list[Document]:
    docs = load_documents(file_path)
    if not docs:
        return []
    if settings.RAG_A_PARENT_ENABLED:
        parents, _ = split_documents_parent_child_with_settings(docs)
        return parents
    return split_documents_with_settings(docs)


def load_and_split(file_path: str) -> list[Document]:
    return load_and_split_for_index(file_path)


def clean_text(text: str) -> str:
    if not text:
        return ""

    text = re.sub(r"[\r\n]+", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)

    lines = text.split("\n")
    cleaned_lines = []
    seen_lines = set()

    for line in lines:
        stripped = line.strip()
        is_structural = (
            stripped.startswith("```")
            or re.match(r"^[\|\-:]+\s*$", stripped)
            or stripped.startswith("##")
            or stripped.startswith("![")
            or stripped in ("", "---", "***")
        )
        if not is_structural and len(stripped) < 5:
            continue
        if not is_structural:
            line_hash = stripped[:150]
            if line_hash in seen_lines:
                continue
            seen_lines.add(line_hash)
        cleaned_lines.append(line)

    return "\n".join(cleaned_lines).strip()
