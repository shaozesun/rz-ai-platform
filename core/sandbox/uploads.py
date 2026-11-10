"""会话级上传文件存储管理 — 用户在对话中上传的文档/表格。

目录结构：{AGENT_ANALYSIS_DATA_DIR}/{thread_id}/uploads/{safe_filename}
按 thread_id 隔离，与 datasets.py 的会话数据集目录同层但不同子目录（uploads 时机早于
analysis_load，还没有 dataset_id）。仅当前会话内持久化，不跨会话/按用户共享。
"""

import logging
import re
import time
from pathlib import Path

from config.settings import settings

logger = logging.getLogger(__name__)

# PPT 用更大的上限（带图汇报稿常见 20~80MB）：这块由 settings.AGENT_UPLOAD_MAX_FILE_MB_PPT 控制
_PPT_EXTS = {'.ppt', '.pptx'}

# 与 datasets.py 一致的安全字符集（用于 thread_id 目录名净化）
_SAFE_RE = re.compile(r'[^A-Za-z0-9_.-]+')


class UploadError(Exception):
  """上传文件不存在、超限或非法。"""


def _session_dir(thread_id: str) -> Path:
  """会话上传目录：DATA_DIR/thread_id/uploads（thread_id 做安全化）。"""
  safe = _SAFE_RE.sub('_', thread_id) or 'default'
  return Path(settings.AGENT_ANALYSIS_DATA_DIR) / safe / 'uploads'


def _normalize_filename(filename: str) -> str:
  """净化用户上传的原始文件名（参照 DeerFlow normalize_filename 的校验顺序）。

  1. 提取 basename，剥离所有路径前缀（防 ../ 穿越、防绝对路径）
  2. 拒绝空/./..
  3. 截断超长文件名（保留扩展名）
  """
  name = Path(filename).name
  if not name or name in ('.', '..'):
    raise UploadError(f'非法文件名: {filename!r}')
  # UTF-8 编码长度上限 200 字节，超长截断（保留扩展名，跨平台文件系统兼容）
  encoded = name.encode('utf-8')
  if len(encoded) > 200:
    suffix = Path(name).suffix
    stem_bytes = encoded[:200 - len(suffix.encode('utf-8'))]
    name = stem_bytes.decode('utf-8', errors='ignore') + suffix
  return name


def _claim_unique_filename(name: str, existing: set) -> str:
  """同名文件自动加序号（参照 DeerFlow claim_unique_filename 的递进后缀策略）。

  name.ext 冲突时依次尝试 name_1.ext / name_2.ext ...
  """
  if name not in existing:
    return name
  stem, suffix = Path(name).stem, Path(name).suffix
  i = 1
  while f'{stem}_{i}{suffix}' in existing:
    i += 1
  return f'{stem}_{i}{suffix}'


def save_upload(thread_id: str, filename: str, content: bytes) -> dict:
  """把上传文件落盘到会话上传目录，超限拒绝。

  Args:
    thread_id: LangGraph 会话 id
    filename: 原始文件名（未净化）
    content: 文件二进制内容

  Returns:
    dict: {filename, size, extension}

  Raises:
    UploadError: 文件名非法、超过单文件大小上限、或会话上传文件数已达上限
  """
  suffix = Path(_normalize_filename(filename)).suffix.lower()
  max_mb = (settings.AGENT_UPLOAD_MAX_FILE_MB_PPT if suffix in _PPT_EXTS
            else settings.AGENT_UPLOAD_MAX_FILE_MB)
  size_mb = len(content) / 1024 / 1024
  if max_mb and size_mb > max_mb:
    logger.warning('上传被拒（超大小上限）: thread=%s file=%s size=%.1fMB limit=%dMB',
                   thread_id, filename, size_mb, max_mb)
    raise UploadError(
      f'文件 {size_mb:.1f}MB 超过 {max_mb:g}MB 上限（{suffix} 类型）；'
      f'可压缩图片或拆分后重试'
    )

  upload_dir = _session_dir(thread_id)
  upload_dir.mkdir(parents=True, exist_ok=True)

  existing_names = {p.name for p in upload_dir.iterdir() if p.is_file()}
  max_files = settings.AGENT_UPLOAD_MAX_FILES_PER_SESSION
  if max_files and len(existing_names) >= max_files:
    logger.warning('上传被拒（会话文件数上限）: thread=%s files=%d limit=%d',
                   thread_id, len(existing_names), max_files)
    raise UploadError(f'本会话上传文件数已达 {max_files} 个上限，请先清理旧对话')

  safe_name = _normalize_filename(filename)
  safe_name = _claim_unique_filename(safe_name, existing_names)

  target = upload_dir / safe_name
  target.write_bytes(content)

  return {
    'filename': safe_name,
    'size': len(content),
    'extension': target.suffix.lower(),
  }


def list_uploads(thread_id: str) -> list[dict]:
  """列出会话内所有已上传文件（按上传时间升序）。

  Returns:
    [{filename, size, extension, uploaded_at}, ...]；无上传目录时返回空列表。
  """
  upload_dir = _session_dir(thread_id)
  if not upload_dir.is_dir():
    return []
  entries = []
  for p in sorted(upload_dir.iterdir(), key=lambda p: p.stat().st_mtime):
    if not p.is_file():
      continue
    try:
      stat = p.stat()
    except OSError:
      continue
    entries.append({
      'filename': p.name,
      'size': stat.st_size,
      'extension': p.suffix.lower(),
      'uploaded_at': int(stat.st_mtime),
    })
  return entries


def get_upload_path(thread_id: str, filename: str) -> Path | None:
  """校验并定位已上传文件路径；非法/不存在返回 None。

  filename 先做 basename 归一（防 ../ 穿越），resolve 后必须仍在会话上传目录内。
  """
  upload_dir = _session_dir(thread_id)
  safe_name = Path(filename).name
  if not safe_name or safe_name in ('.', '..'):
    return None
  target = (upload_dir / safe_name).resolve()
  base = upload_dir.resolve()
  if not target.is_relative_to(base) or not target.is_file():
    return None
  return target
