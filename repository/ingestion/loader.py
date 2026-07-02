"""
文档加载器：支持多种文件格式的加载（优化版本）
"""
from __future__ import annotations

from pathlib import Path
from langchain_core.documents import Document
import pandas as pd
from pypdf import PdfReader
from docx2txt import process
import re
import logging
import tempfile
import subprocess
import json
from collections import defaultdict

from config.settings import settings

logger = logging.getLogger(__name__)


# SOP/EOP/MOP 模板段落关键词（用于结构化 XLSX 解析）
_SECTION_PATTERNS: list[tuple[str, str]] = [
    ("cover", r"封面|文档编号|RZKJ-LF"),
    ("version", r"版本控制|修订记录|变更记录|版本号|修订|版本历史"),
    ("risk", r"风险评估|风险识别|危险源|风险等级|风险分析|安全风险"),
    ("prerequisites", r"操作前检查|前置条件|准备工作|开机条件|启动条件|操作条件"),
    ("tools", r"所需工具|仪器仪表|工具清单|所需仪表|所需仪器|工具材料|备品备件"),
    ("steps", r"操作步骤|操作流程|操作过程|工作步骤|执行步骤|处理步骤|维护步骤|应急步骤|操作程序"),
    ("rollback", r"回滚方案|回退方案|应急措施|恢复方案|异常处理|终止条件"),
    ("signoff", r"签字确认|签发|审批|批准|确认签字|操作确认"),
]

# 文件路径中提取 doc_type 的模式
_DOC_TYPE_PATTERNS: list[tuple[str, str]] = [
    ("SOP", r"SOP|标准操作|sop|Sop"),
    ("EOP", r"EOP|应急操作|eop|Eop"),
    ("MOP", r"MOP|维护操作|mop|Mop"),
]

# 文件路径中提取 subsystem 的模式
_SUBSYSTEM_PATTERNS: list[tuple[str, str]] = [
    ("供配电", r"供配电|配电|电源|电力|电气"),
    ("制冷", r"制冷|冷却|空调|暖通"),
    ("智能化", r"智能化|消防|门禁|监控|安防|报警"),
]


def extract_doc_metadata(file_path: str) -> dict[str, str]:
    """从文件路径中提取 doc_type 和 subsystem。

    Returns:
        {"doc_type": "...", "subsystem": "..."}
        匹配不到时返回 "general"，确保所有 chunk 都有元数据字段，
        避免意图过滤器中被直接排除。
    """
    path_str = str(file_path)
    doc_type = "general"
    subsystem = "general"
    for dt, pattern in _DOC_TYPE_PATTERNS:
        if re.search(pattern, path_str):
            doc_type = dt
            break
    for sub, pattern in _SUBSYSTEM_PATTERNS:
        if re.search(pattern, path_str):
            subsystem = sub
            break
    return {"doc_type": doc_type, "subsystem": subsystem}


def _detect_section_type(row_text: str) -> str | None:
    """检测行文本是否匹配某个模板段落。"""
    text = (row_text or "").strip()
    if not text or len(text) < 3:
        return None
    for section_type, pattern in _SECTION_PATTERNS:
        if re.search(pattern, text):
            return section_type
    return None


def clean_text(text: str) -> str:
    """
    清理文本，去除噪声和重复内容。

    保留 markdown 结构化元素（代码围栏、表格分隔线等），
    避免破坏 Mermaid 代码块和 Markdown 表格。
    """
    if not text:
        return ""

    import re

    # 去除多余空白字符（保留代码围栏内的缩进）
    text = re.sub(r"[\r\n]+", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)

    # 去除重复的页眉页脚模式
    lines = text.split("\n")
    cleaned_lines = []
    seen_lines = set()

    for line in lines:
        stripped = line.strip()
        # 保留 markdown 结构化短行：代码围栏、表格分隔线、空标题等
        is_structural = (
            stripped.startswith("```")  # 代码围栏
            or re.match(r"^[\|\-:]+\s*$", stripped)  # markdown 表格分隔线
            or stripped.startswith("##")  # 标题
            or stripped.startswith("![")  # 图片引用
            or stripped in ("", "---", "***")  # 空行、分隔线
        )
        if not is_structural and len(stripped) < 5:
            continue
        # 结构化行不做去重（避免多个 Mermaid/表格被误删）
        if not is_structural:
            line_hash = stripped[:150]
            if line_hash in seen_lines:
                continue
            seen_lines.add(line_hash)
        cleaned_lines.append(line)

    return "\n".join(cleaned_lines).strip()


def load_documents(file_path: str) -> list[Document]:
    """
    根据文件扩展名加载文档。

    Args:
        file_path: 文件路径

    Returns:
        List[Document]: 加载的文档列表

    Raises:
        ValueError: 不支持的文件类型
        FileNotFoundError: 文件不存在
    """
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"文件不存在: {file_path}")

    suffix = path.suffix.lower()

    if suffix == ".pdf":
        return _load_pdf_cached(file_path)
    elif suffix == ".docx":
        return _load_docx(file_path)
    elif suffix == ".xlsx" or suffix == ".xls":
        return _load_excel(file_path)
    elif suffix == ".csv":
        return _load_csv(file_path)
    elif suffix == ".txt":
        return _load_txt(file_path)
    elif suffix == ".md":
        return _load_md(file_path)
    else:
        raise ValueError(f"不支持的文件类型: {suffix}")


def _describe_image(image_path: str, caption: str = "", footnote: str = "") -> str:
    """使用多模态模型生成图片文字描述。"""
    import base64
    import asyncio

    from core.model_gateway import model_gateway

    with open(image_path, "rb") as f:
        image_data = base64.b64encode(f.read()).decode("utf-8")

    context_parts = ["请用中文描述这张图片的内容。"]
    if caption:
        context_parts.append(f"图片标题：{caption}")
    if footnote:
        context_parts.append(f"图片脚注：{footnote}")
    context_parts.append(
        "如果这是一个流程图、架构图或数据图表，请同时做两件事：\n"
        "1. 用 Mermaid 语法描述该图的结构（flowchart TD/LR、graph、sequenceDiagram 等），"
        "确保 Mermaid 代码可直接渲染；\n"
        "2. 另起一段用文字简要说明图中展示的流程、关系和关键信息。\n"
        "如果图片包含表格数据，请用 Markdown 表格格式输出。"
    )

    async def _run() -> str:
        return await model_gateway.vision(
            system_prompt="你是一个专业的技术文档分析助手，擅长描述图片内容并生成结构化输出。",
            user_text="\n".join(context_parts),
            images=[image_data],
            temperature=0.0,
        )

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_run())
    else:
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor() as pool:
            return pool.submit(asyncio.run, _run()).result()


def _extract_mermaid_after_image(md_text: str, match_end: int) -> str | None:
    """匹配图片后的 <details> 块中的 Mermaid 代码。

    若图片后面紧跟 <details>...</details> 且内部含 ```mermaid ... ```，
    则返回 Mermaid 代码块，否则返回 None。
    """
    tail = md_text[match_end:match_end + 3000]
    m = re.match(
        r'\s*\n\s*<details>\s*\n?\s*<summary>.*?</summary>\s*\n?\s*'
        r'```mermaid\s*\n(.*?)```\s*\n?\s*</details>',
        tail, re.DOTALL,
    )
    if m:
        return m.group(1).strip()
    return None


def _process_markdown_images(md_text: str) -> str:
    """将 markdown 中的 ![](images/xxx.jpg) 替换为内联的结构化内容。

    - 图片后面有 <details> 含 Mermaid → 用 Mermaid 代码替换图片引用
    - 去掉 <details> 包装，保留 Mermaid 代码块
    - 无 Mermaid 的图片保留占位标记
    """
    img_re = re.compile(r'!\[.*?\]\((images/[^)]+)\)')

    def _replace_img(m: re.Match) -> str:
        mermaid_code = _extract_mermaid_after_image(md_text, m.end())
        if mermaid_code:
            return f"\n\n```mermaid\n{mermaid_code}\n```\n"
        return f"\n\n[图片：{m.group(1)}]\n\n"

    # 替换图片引用
    result = img_re.sub(_replace_img, md_text)

    # 清洗掉已被内联的 <details> 包装（保留内容已在上面替换为 Mermaid 代码块）
    result = re.sub(
        r'\n\s*<details>\s*\n?\s*<summary>.*?</summary>\s*\n?\s*'
        r'```mermaid\s*\n.*?```\s*\n?\s*</details>',
        '', result, flags=re.DOTALL,
    )

    return result


def _replace_images_in_markdown(md_text: str, md_dir: Path, content_list: list) -> str:
    """将 markdown 中的 ![image](...) 引用替换为结构化内容。

    优先使用 markdown 内自带的 Mermaid 流程图，不再调用多模态 LLM。
    用于 fallback 路径：当 content_list 中没有文本条目时，
    将 markdown 中的图片引用替换后以整篇 markdown 作为 Document。
    """
    # 新版：直接用 markdown 内置的 Mermaid/结构化内容，无需 content_list
    if not content_list:
        return _process_markdown_images(md_text)

    # 旧版兼容：content_list 的 caption/footnote 降级
    image_meta: dict[str, dict[str, str]] = {}
    for item in content_list:
        if isinstance(item, dict) and item.get("type") == "image" and item.get("img_path"):
            image_meta[item["img_path"]] = {
                "caption": " ".join(item.get("image_caption", []) or []),
                "footnote": " ".join(item.get("image_footnote", []) or []),
            }

    def _replace_match(m: re.Match) -> str:
        img_rel_path = m.group(1)
        mermaid_code = _extract_mermaid_after_image(md_text, m.end())
        if mermaid_code:
            return f"\n\n```mermaid\n{mermaid_code}\n```\n"
        meta = image_meta.get(img_rel_path, {})
        caption = meta.get("caption", "")
        footnote = meta.get("footnote", "")
        parts = []
        if caption:
            parts.append(f"图片标题：{caption}")
        if footnote:
            parts.append(f"图片脚注：{footnote}")
        if parts:
            return f"\n\n[{'；'.join(parts)}]\n\n"
        return f"\n\n[图片：{img_rel_path}]\n\n"

    return img_re.sub(_replace_match, md_text)


def _parse_images_to_docs(content_list: list, file_path: str, md_dir: Path) -> list[Document]:
    """遍历 content_list 中的 image 条目，生成图片文字描述并返回 Document 列表。

    每个图片生成一个独立的 Document，metadata 中标记 content_type="image_description"，
    便于后续检索时区分文本内容和图片描述。
    调用失败时降级使用 MinerU 提取的 caption/footnote。
    """
    docs: list[Document] = []
    for item in content_list:
        if not isinstance(item, dict):
            continue
        if item.get("type") != "image":
            continue
        img_path = item.get("img_path", "")
        if not img_path:
            continue
        full_path = md_dir / img_path
        if not full_path.exists():
            logger.warning("图片文件不存在: %s", full_path)
            continue

        caption = " ".join(item.get("image_caption", []) or [])
        footnote = " ".join(item.get("image_footnote", []) or [])

        try:
            desc = _describe_image(str(full_path), caption, footnote)
            if desc:
                docs.append(Document(
                    page_content=desc,
                    metadata={
                        "source": file_path,
                        "page": item.get("page_idx", 0) + 1,
                        "parser": "mineru",
                        "image_path": img_path,
                        "content_type": "image_description",
                    },
                ))
                logger.info("图片描述生成成功: %s", img_path)
                continue
        except Exception as e:
            logger.warning("图片描述失败 %s: %s", img_path, e)

        # 降级：使用 caption/footnote
        fallback_parts = []
        if caption:
            fallback_parts.append(f"图片标题：{caption}")
        if footnote:
            fallback_parts.append(f"图片脚注：{footnote}")
        if fallback_parts:
            docs.append(Document(
                page_content="\n".join(fallback_parts),
                metadata={
                    "source": file_path,
                    "page": item.get("page_idx", 0) + 1,
                    "parser": "mineru",
                    "image_path": img_path,
                    "content_type": "image_description",
                },
            ))

    return docs


def _process_api_images(md_text: str, markdown_url: str) -> str:
    """下载 MinerU API 返回的 markdown 中的图片，替换为多模态 LLM 的文字描述。

    markdown_url 用于解析相对路径的图片 URL。
    下载失败时保留原始图片引用，不阻塞整体流程。
    """
    import requests
    from urllib.parse import urljoin

    # 构建图片 URL 的 base（用于解析相对路径）
    base_url = markdown_url.rsplit("/", 1)[0] + "/" if markdown_url else ""

    def _download_and_describe(m: re.Match) -> str:
        img_url = m.group(1)
        # 解析为绝对 URL
        if not img_url.startswith(("http://", "https://")):
            img_url = urljoin(base_url, img_url)

        try:
            img_resp = requests.get(img_url, timeout=30)
            img_resp.raise_for_status()
        except Exception as e:
            logger.warning("图片下载失败 %s: %s", img_url, e)
            return m.group(0)  # 保留原始引用

        # 写入临时文件并调用 _describe_image
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
            tmp.write(img_resp.content)
            tmp_path = tmp.name

        try:
            desc = _describe_image(tmp_path)
            if desc:
                return f"\n\n[图片描述：{desc}]\n\n"
        except Exception as e:
            logger.warning("图片描述失败 %s: %s", img_url, e)
        finally:
            try:
                Path(tmp_path).unlink()
            except OSError:
                pass

        return m.group(0)  # 降级：保留原始引用

    return re.sub(r'!\[.*?\]\(([^)]+)\)', _download_and_describe, md_text)


# 缓存 _load_pdf 结果，避免同一次上传中 add_file 和 export_chunks 重复解析
_pdf_load_cache: dict[str, list[Document]] = {}


def _load_pdf_cached(file_path: str) -> list[Document]:
    """带缓存的 PDF 加载，同一路径在会话内只解析一次。"""
    if file_path not in _pdf_load_cache:
        _pdf_load_cache[file_path] = _load_pdf_uncached(file_path)
    return _pdf_load_cache[file_path]


def _load_pdf_uncached(file_path: str) -> list[Document]:
    """加载 PDF：优先使用 MinerU API，其次 CLI，失败降级到 pypdf"""
    if settings.MINERU_ENABLED:
        if settings.MINERU_API_BASE:
            try:
                return _load_pdf_mineru_api(file_path)
            except Exception as e:
                logger.warning("MinerU API 解析失败，尝试 CLI: %s", e)
        try:
            return _load_pdf_mineru(file_path)
        except Exception as e:
            logger.warning("MinerU 解析失败，降级到 pypdf: %s", e)
    return _load_pdf_pypdf(file_path)


def _load_pdf_mineru_api(file_path: str) -> list[Document]:
    """通过 MinerU v4 API 解析 PDF（无需本地 CLI）。

    流程：
      1. POST /api/v4/file-urls/batch → 获取 batch_id + 上传 URL
      2. PUT 本地文件到 OSS
      3. 轮询 GET /api/v4/extract-results/batch/{batch_id} 直到 done
      4. 下载 full_zip_url → 解压 → 解析 content_list + images/

    要求：MINERU_API_BASE 和 MINERU_API_TOKEN 均需在 .env 中配置。
    """
    import requests
    import time
    import zipfile
    import io

    api_base = settings.MINERU_API_BASE.rstrip("/")
    token = settings.MINERU_API_TOKEN
    if not token:
        raise RuntimeError("MINERU_API_TOKEN 未配置")
    file_name = Path(file_path).name
    file_size = Path(file_path).stat().st_size
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    logger.info(
        "MinerU v4 API 开始解析: %s (%.1f MB)", file_name, file_size / 1024 / 1024,
    )

    # —— Step 1: 申请上传 URL ——
    batch_resp = requests.post(
        f"{api_base}/api/v4/file-urls/batch",
        json={
            "files": [{"name": file_name}],
            "model_version": "vlm",
            "return_images": "true",
            "image_extract": "true",
            "no_cache": "true",
        },
        headers=headers,
        timeout=30,
    )
    batch_resp.raise_for_status()
    batch_data = batch_resp.json()
    logger.info("MinerU v4 batch 响应: %s", json.dumps(batch_data, ensure_ascii=False)[:500])
    if batch_data.get("code") != 0:
        raise RuntimeError(f"v4 申请上传失败: {batch_data.get('msg')}")

    batch_id = batch_data["data"]["batch_id"]
    file_urls = batch_data["data"]["file_urls"]

    # —— Step 2: 上传本地文件 ——
    with open(file_path, "rb") as f:
        upload_resp = requests.put(file_urls[0], data=f, timeout=120)
    if upload_resp.status_code != 200:
        raise RuntimeError(f"v4 文件上传失败: HTTP {upload_resp.status_code}")
    logger.info("MinerU v4: 文件上传完成")

    # —— Step 3: 轮询结果 ——
    poll_interval = settings.MINERU_API_POLL_INTERVAL
    max_poll = settings.MINERU_API_MAX_POLL
    zip_url = None

    for attempt in range(max_poll):
        time.sleep(poll_interval)
        result_resp = requests.get(
            f"{api_base}/api/v4/extract-results/batch/{batch_id}",
            headers=headers,
            timeout=30,
        )
        result_resp.raise_for_status()
        result_data = result_resp.json()

        if result_data.get("code") != 0:
            logger.debug("MinerU v4 轮询 %d: code=%s", attempt + 1, result_data.get("code"))
            continue

        extract_results = result_data.get("data", {}).get("extract_result", [])
        if not extract_results:
            continue

        item = extract_results[0]
        state = item.get("state", "")

        if state == "done":
            zip_url = item.get("full_zip_url")
            if zip_url:
                logger.info("MinerU v4 解析完成: %ds", (attempt + 1) * poll_interval)
                break
            else:
                raise RuntimeError("v4 返回 done 但缺少 full_zip_url")
        elif state == "failed":
            err_msg = item.get("err_msg", "未知错误")
            raise RuntimeError(f"v4 解析失败: {err_msg}")

        logger.debug("MinerU v4 轮询 %d: state=%s", attempt + 1, state)

    if not zip_url:
        raise RuntimeError(f"MinerU v4 轮询超时 ({max_poll * poll_interval}s)")

    # —— Step 4: 下载并解压结果 ——
    logger.info("MinerU v4 下载结果: %s", zip_url[:100])
    zip_resp = requests.get(zip_url, timeout=120)
    zip_resp.raise_for_status()

    pdf_stem = Path(file_path).stem

    with tempfile.TemporaryDirectory(prefix="mineru_v4_") as tmpdir:
        md_dir = Path(tmpdir) / pdf_stem

        with zipfile.ZipFile(io.BytesIO(zip_resp.content)) as zf:
            zf.extractall(md_dir)

        # 定位 .md 文件
        md_files = list(md_dir.rglob("*.md"))
        if not md_files:
            raise RuntimeError("v4 ZIP 中未找到 .md 文件")
        markdown_text = md_files[0].read_text(encoding="utf-8")

        # DEBUG: 保存 MinerU 原始输出
        _debug_dir = Path(settings.UPLOAD_DIR).parent / 'debug_mineru_output'
        _debug_dir.mkdir(exist_ok=True)
        _debug_stem = Path(file_path).stem
        (_debug_dir / f'{_debug_stem}_01_raw.md').write_text(markdown_text, encoding='utf-8')

        # 处理图片：用 markdown 内置的 Mermaid 流程图替换图片引用
        processed_md = _process_markdown_images(markdown_text)
        (_debug_dir / f'{_debug_stem}_02_processed.md').write_text(processed_md, encoding='utf-8')

        cleaned = clean_text(processed_md)
        (_debug_dir / f'{_debug_stem}_03_cleaned.md').write_text(cleaned, encoding='utf-8')
        docs = [Document(
            page_content=cleaned,
            metadata={"source": file_path, "parser": "mineru_api_v4"},
        )]

        logger.info(
            "MinerU v4 解析完成: %s → markdown %d chars",
            file_name,
            len(cleaned),
        )
        return docs


def _load_pdf(file_path: str) -> list[Document]:
    """向后兼容别名，实际走缓存版本。"""
    return _load_pdf_cached(file_path)


def _find_mineru_bin() -> str:
    """查找 mineru CLI 可执行文件路径。"""
    import shutil
    import sys
    # 优先 PATH
    found = shutil.which("mineru")
    if found:
        return found
    # 当前 Python 同目录下（conda env 场景）
    bin_dir = Path(sys.executable).parent
    candidate = bin_dir / "mineru"
    if candidate.exists():
        return str(candidate)
    return "mineru"  # 降级


def _load_pdf_mineru(file_path: str) -> list[Document]:
    """
    使用 MinerU 将 PDF 转为 Markdown，再解析为 Document 列表。

    通过 subprocess 调用 mineru CLI：
        mineru -p file.pdf -o tmpdir -b pipeline
    然后读取生成的 .md 和 content_list.json，按页转换为 Document。
    """
    with tempfile.TemporaryDirectory(prefix="mineru_") as tmpdir:
        # 优先使用 PATH 中的 mineru，找不到则用当前 Python 同目录下的
        mineru_bin = _find_mineru_bin()
        cmd = [
            mineru_bin,
            "-p", file_path,
            "-o", tmpdir,
            "-b", settings.MINERU_BACKEND,
            "-l", settings.MINERU_LANG,
        ]
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=settings.MINERU_TIMEOUT,
        )
        if result.returncode != 0:
            raise RuntimeError(f"MinerU 解析失败 (exit={result.returncode}): {result.stderr}")

        # 定位生成的 .md 文件
        pdf_stem = Path(file_path).stem
        md_path = Path(tmpdir) / pdf_stem / f"{pdf_stem}.md"
        if not md_path.exists():
            # 3.x 版本可能直接放在 tmpdir 下，或者文件名不同
            candidates = list(Path(tmpdir).rglob("*.md"))
            if not candidates:
                raise RuntimeError("MinerU 未生成 markdown 输出")
            md_path = candidates[0]

        markdown_text = md_path.read_text(encoding="utf-8")

        # DEBUG: 保存 MinerU CLI 原始输出
        _debug_dir = Path(settings.UPLOAD_DIR).parent / 'debug_mineru_output'
        _debug_dir.mkdir(exist_ok=True)
        _debug_stem = Path(file_path).stem
        (_debug_dir / f'{_debug_stem}_01_raw.md').write_text(markdown_text, encoding='utf-8')

        # 处理图片：用 markdown 内置的 Mermaid 流程图替换图片引用
        processed_md = _process_markdown_images(markdown_text)
        (_debug_dir / f'{_debug_stem}_02_processed.md').write_text(processed_md, encoding='utf-8')

        cleaned = clean_text(processed_md)
        (_debug_dir / f'{_debug_stem}_03_cleaned.md').write_text(cleaned, encoding='utf-8')

        docs = [Document(
            page_content=cleaned,
            metadata={"source": file_path, "parser": "mineru"},
        )]

        logger.info(
            "MinerU CLI 解析完成: %s → markdown %d chars",
            Path(file_path).name,
            len(cleaned),
        )
        return docs


def _parse_content_list_to_docs(content_list: list, file_path: str, md_dir: Path | None = None) -> list[Document]:
    """将 MinerU 的 content_list 按页分组为 Document 列表。

    content_list 中每个元素包含:
      - type: "text" | "table" | "image" | "equation" 等
      - text: 文本内容（表格/公式保留 markdown 格式）
      - img_path / image_caption / image_footnote: 图片相关字段
      - page_idx: 页码

    图片描述会被内联到同页文本中，确保标题和对应的流程图不会被切碎到不同 chunk。
    """
    pages: dict[int, list[str]] = defaultdict(list)
    for item in content_list:
        if not isinstance(item, dict):
            continue
        page = item.get("page_idx", 0)
        item_type = item.get("type", "")

        if item_type == "image" and md_dir:
            img_path = item.get("img_path", "")
            if img_path:
                full_path = md_dir / img_path
                if full_path.exists():
                    caption = " ".join(item.get("image_caption", []) or [])
                    footnote = " ".join(item.get("image_footnote", []) or [])
                    try:
                        desc = _describe_image(str(full_path), caption, footnote)
                        if desc:
                            pages[page].append(f"[图片描述] {desc}")
                            continue
                    except Exception as e:
                        logger.warning("图片描述失败 %s: %s", img_path, e)
                    # 降级：caption/footnote
                    fallback = []
                    if caption:
                        fallback.append(f"图片标题：{caption}")
                    if footnote:
                        fallback.append(f"图片脚注：{footnote}")
                    if fallback:
                        pages[page].append("[图片] " + "；".join(fallback))
                else:
                    logger.warning("图片文件不存在: %s", full_path)
        else:
            text = (item.get("text") or "").strip()
            if text:
                pages[page].append(text)

    docs: list[Document] = []
    for page_idx in sorted(pages.keys()):
        page_text = "\n\n".join(pages[page_idx])
        cleaned = clean_text(page_text)
        if cleaned:
            docs.append(Document(
                page_content=cleaned,
                metadata={
                    "source": file_path,
                    "page": page_idx + 1,
                    "parser": "mineru",
                },
            ))
    return docs


def _load_pdf_pypdf(file_path: str) -> list[Document]:
    """加载 PDF 文件（pypdf 降级方案）"""
    reader = PdfReader(file_path)
    docs = []
    for page_num, page in enumerate(reader.pages):
        text = page.extract_text()
        if text:
            text = clean_text(text)
            if text:
                docs.append(Document(
                    page_content=text,
                    metadata={
                        "source": file_path,
                        "page": page_num + 1,
                    },
                ))
    return docs


def _load_docx(file_path: str) -> list[Document]:
    """加载 Word 文档（优化版本）"""
    text = process(file_path)
    text = clean_text(text)
    if not text:
        return []
    return [Document(page_content=text, metadata={"source": file_path})]


def _load_excel(file_path: str) -> list[Document]:
    """加载 Excel 文件（支持多 sheet，合并单元格处理）"""
    # 使用 openpyxl 加载 .xlsx 文件
    if file_path.lower().endswith('.xlsx'):
        return _load_excel_xlsx(file_path)
    # 使用 pandas 加载 .xls 文件
    else:
        return _load_excel_xls(file_path)


def _load_csv(file_path: str) -> list[Document]:
    """加载 CSV 文件"""
    df = pd.read_csv(file_path)
    text = df.to_string(index=False)
    return [Document(page_content=text, metadata={"source": file_path})]


def _load_txt(file_path: str) -> list[Document]:
    """加载文本文件（优化版本）"""
    with open(file_path, "r", encoding="utf-8") as f:
        text = f.read()
    text = clean_text(text)
    if not text:
        return []
    return [Document(page_content=text, metadata={"source": file_path})]


def _load_md(file_path: str) -> list[Document]:
    """加载 Markdown 文件（优化版本）"""
    with open(file_path, "r", encoding="utf-8") as f:
        text = f.read()
    text = clean_text(text)
    if not text:
        return []
    return [Document(page_content=text, metadata={"source": file_path})]


# 声明/页脚类重复句：整行主要是该内容时只保留一次，减少向量库噪音
_BOILERPLATE_PREFIX = "本文中的所有信息均为润泽科技数据中心内部公开信息"


def _is_boilerplate_line(line: str) -> bool:
    """判断是否为声明类页脚（可扩展更多关键词）"""
    s = line.strip()
    if not s or len(s) < 10:
        return False
    return _BOILERPLATE_PREFIX in s or s.startswith("本文中的所有信息")


def _dedupe_boilerplate_in_line(line: str) -> str:
    """若一行内声明段出现多次，只保留一段（用于多列同内容拼成一行时）"""
    s = line.strip()
    if not s or _BOILERPLATE_PREFIX not in s:
        return line
    if s.count(_BOILERPLATE_PREFIX) <= 1:
        return line
    # 取第一段完整句（从「本文」到第一个句号）
    start = s.find(_BOILERPLATE_PREFIX)
    if start == -1:
        return line
    end = s.find("。", start)
    if end == -1:
        end = len(s)
    else:
        end += 1
    return s[start:end]


def _cell_value(val) -> str:
    """将单元格值转为字符串，None/NaN 为空串"""
    if val is None:
        return ""
    if hasattr(val, "__iter__") and not isinstance(val, (str, bytes)):
        try:
            return " ".join(str(x) for x in val if x is not None)
        except Exception:
            pass
    s = str(val).strip()
    return s if s and s.lower() != "nan" else ""


def _load_excel_xlsx(file_path: str) -> list[Document]:
    """
    使用 openpyxl 加载 .xlsx：支持多 sheet，合并单元格展开后按行拼成文本。
    对于 SOP/EOP/MOP 模板文档，自动识别段落类型并添加 section_type 等元数据。
    """
    try:
        from openpyxl import load_workbook

        doc_meta = extract_doc_metadata(file_path)
        wb = load_workbook(file_path, data_only=True)
        docs: list[Document] = []
        try:
            for sheet_name in wb.sheetnames:
                ws = wb[sheet_name]
                if ws.max_row < 1 or ws.max_column < 1:
                    continue
                # 合并单元格：(row, col) -> 左上角的值
                merge_map = {}
                for mr in ws.merged_cells.ranges:
                    try:
                        top_left = ws.cell(mr.min_row, mr.min_col).value
                        for r in range(mr.min_row, mr.max_row + 1):
                            for c in range(mr.min_col, mr.max_col + 1):
                                merge_map[(r, c)] = top_left
                    except Exception:
                        continue

                # 按段落分组：检测 section header，将内容按 section_type 分段
                sections: dict[str, list[str]] = {}
                current_section = "__header__"
                sections[current_section] = []

                for r in range(1, ws.max_row + 1):
                    cells = []
                    for c in range(1, ws.max_column + 1):
                        val = merge_map.get((r, c))
                        if val is None:
                            val = ws.cell(r, c).value
                        cells.append(_cell_value(val))

                    # 行内连续相同单元格只保留一个
                    deduped_cells = []
                    for c in cells:
                        if deduped_cells and deduped_cells[-1] == c:
                            continue
                        deduped_cells.append(c)
                    row_text = " ".join(deduped_cells)
                    row_text = _dedupe_boilerplate_in_line(row_text)

                    detected = _detect_section_type(row_text)
                    if detected:
                        current_section = detected
                        if current_section not in sections:
                            sections[current_section] = []
                    sections.setdefault(current_section, []).append(row_text)

                # 为每个有内容的 section 创建 Document
                for section_type, rows in sections.items():
                    # 去重
                    deduped = []
                    boilerplate_seen = False
                    for line in rows:
                        s = line.strip()
                        if s and deduped and deduped[-1] == line:
                            continue
                        if s and _is_boilerplate_line(line):
                            if boilerplate_seen:
                                continue
                            boilerplate_seen = True
                        deduped.append(line)
                    full_text = "\n".join(deduped).strip()
                    if not full_text:
                        continue

                    metadata = {
                        "sheet": sheet_name,
                        "source": file_path,
                        "section_type": section_type,
                    }
                    metadata.update(doc_meta)
                    docs.append(Document(page_content=full_text, metadata=metadata))

        finally:
            if getattr(wb, "close", None):
                wb.close()

        if docs and doc_meta:
            logger.info(
                "[Loader] 结构化解析 %s: doc_type=%s subsystem=%s sections=%d",
                Path(file_path).name,
                doc_meta.get("doc_type", "-"),
                doc_meta.get("subsystem", "-"),
                len(set(d.metadata.get("section_type", "") for d in docs)),
            )
        return docs
    except Exception:
        # 如果 openpyxl 加载失败，回退到 pandas
        return _load_excel_xls(file_path)


def _load_excel_xls(file_path: str) -> list[Document]:
    """
    .xls 使用 pandas 读取，支持多 sheet；按行拼接，放宽空行过滤。
    """
    dfs = pd.read_excel(file_path, sheet_name=None)
    docs: list[Document] = []
    for sheet_name, df in dfs.items():
        if df.empty:
            continue
        for i, row in df.iterrows():
            parts = [f"{k}: {v}" for k, v in row.items() if pd.notna(v) and _cell_value(v)]
            text = " ".join(parts)
            if not text.strip():
                continue
            docs.append(
                Document(
                    page_content=text,
                    metadata={"sheet": sheet_name, "row": i, "source": file_path},
                )
            )
    return docs