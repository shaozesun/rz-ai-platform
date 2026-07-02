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

# MinerU/Markdown 文档专用：优先在标题边界切分，保持章节语义完整
MARKDOWN_SEPARATORS = [
    "\n## ",
    "\n### ",
    "\n#### ",
    "\n# ",
    *DEFAULT_SEPARATORS,
]


def _doc_is_markdown(documents: list[Document]) -> bool:
    """判断文档是否为 MinerU/Markdown 产物。"""
    if not documents:
        return False
    parser = str((documents[0].metadata or {}).get("parser", ""))
    return parser.startswith("mineru")


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
    splits = text_splitter.split_documents(documents)
    return merge_small_chunks(
        splits,
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

        raw_parents = parent_splitter.split_documents([doc])
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

            child_texts = [
                t.strip() for t in child_splitter.split_text(parent_text) if t.strip()
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
