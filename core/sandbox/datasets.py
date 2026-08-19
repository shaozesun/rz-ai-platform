"""数据集存储管理 — analysis_load 落盘 / analysis_exec 读取。

目录结构：{AGENT_ANALYSIS_DATA_DIR}/{thread_id}/{dataset_id}/
  - data.csv    全量记录（沙箱子进程 cwd 读取）
  - meta.json   元数据（来源能力、参数、行数、列名、时间）

按 thread_id（LangGraph 会话）隔离；每会话数据集数量受上限约束，超限删最旧。
"""

import json
import re
import time
import uuid
from pathlib import Path

from config.settings import settings

# 数据集 id 允许字符（用于生成子目录名，防路径穿越）
_SAFE_RE = re.compile(r'[^A-Za-z0-9_.-]+')

# 预览单元格最大字符数（只回摘要，不回原始全量）
_PREVIEW_CELL_CHARS = 80
_PREVIEW_ROWS = 5


class DatasetError(Exception):
  """数据集不存在或非法。"""


def _session_dir(thread_id: str) -> Path:
  """会话目录：DATA_DIR/thread_id（thread_id 做安全化）。"""
  safe = _SAFE_RE.sub('_', thread_id) or 'default'
  return Path(settings.AGENT_ANALYSIS_DATA_DIR) / safe


def _evict_if_needed(thread_dir: Path, dataset_id: str) -> None:
  """每会话数据集数超上限时删最旧（保留刚写入的当前数据集）。"""
  max_count = settings.AGENT_ANALYSIS_MAX_DATASETS_PER_SESSION
  if not thread_dir.is_dir():
    return
  entries = sorted(
    thread_dir.iterdir(),
    key=lambda p: p.stat().st_mtime if p.is_dir() else 0,
    reverse=True,
  )
  keep = [p for p in entries if p.name != dataset_id][:max_count - 1]
  for p in entries:
    if p.name != dataset_id and p not in keep:
      for f in p.iterdir():
        f.unlink(missing_ok=True)
      p.rmdir()


def save_dataset(
  thread_id: str,
  capability_id: str,
  params: dict | None,
  records: list[dict],
  dataset_name: str = 'data',
) -> dict:
  """把全量记录落盘为数据集。

  Args:
    thread_id: LangGraph 会话 id
    capability_id: 数据来源能力 id
    params: 拉取参数
    records: 记录列表（dict）
    dataset_name: 数据集展示名，用于生成 dataset_id 前缀

  Returns:
    dict: {dataset_id, rows, columns, preview}
  """
  import pandas as pd

  name_safe = _SAFE_RE.sub('_', dataset_name) or 'data'
  dataset_id = f'{name_safe}-{uuid.uuid4().hex[:8]}'

  thread_dir = _session_dir(thread_id)
  ds_dir = thread_dir / dataset_id
  ds_dir.mkdir(parents=True, exist_ok=True)

  df = pd.DataFrame(records)
  df.to_csv(ds_dir / 'data.csv', index=False)

  meta = {
    'dataset_id': dataset_id,
    'capability_id': capability_id,
    'params': params or {},
    'rows': len(records),
    'columns': list(df.columns) if not df.empty else [],
    'created_at': int(time.time()),
  }
  (ds_dir / 'meta.json').write_text(
    json.dumps(meta, ensure_ascii=False, indent=2), encoding='utf-8'
  )

  _evict_if_needed(thread_dir, dataset_id)

  return {
    'dataset_id': dataset_id,
    'rows': len(records),
    'columns': meta['columns'],
    'preview': _build_preview(records),
  }


def _build_preview(records: list[dict]) -> list[dict]:
  """前 N 行摘要，单元格截断——只回摘要，不进 LLM 上下文。"""
  preview = []
  for rec in records[:_PREVIEW_ROWS]:
    row = {}
    for k, v in rec.items():
      s = str(v)
      row[k] = s[:_PREVIEW_CELL_CHARS] + ('…' if len(s) > _PREVIEW_CELL_CHARS else '')
    preview.append(row)
  return preview


def load_dataset_dir(thread_id: str, dataset_id: str) -> Path:
  """定位数据集目录；不存在抛 DatasetError（友好提示）。"""
  ds_dir = _session_dir(thread_id) / dataset_id
  if not (ds_dir / 'data.csv').is_file():
    raise DatasetError(
      f'数据集 {dataset_id} 不存在或已过期。请先用 analysis_load 拉取数据。'
    )
  return ds_dir


def find_artifact_dir(thread_id: str, filename: str) -> Path | None:
  """按文件名在会话数据集目录里定位最新产物所在目录；找不到返回 None。

  用于修复 LLM 编造的 <file url> / 兜底下载端点：编造 URL 里的 dataset_id
  不可信，只能按文件名扫会话目录。filename 先做 basename 归一，防 `..` 穿越。
  """
  filename = Path(filename).name
  thread_dir = _session_dir(thread_id)
  best: Path | None = None
  best_mtime = -1.0
  if thread_dir.is_dir():
    for ds_dir in thread_dir.iterdir():
      if ds_dir.is_dir() and (ds_dir / filename).is_file():
        mtime = ds_dir.stat().st_mtime
        if mtime > best_mtime:
          best_mtime, best = mtime, ds_dir
  return best


def resolve_artifact_url(thread_id: str, filename: str) -> str | None:
  """按文件名在会话数据集目录里解析产物的权威下载 URL；找不到返回 None。

  用于修复 LLM 编造的 <file url> —— 落库前按真实文件反查权威地址。
  """
  ds_dir = find_artifact_dir(thread_id, filename)
  if ds_dir is None:
    return None
  return f'/api/v1/chat/agent/artifacts/{thread_id}/{ds_dir.name}/{filename}'


def read_meta(thread_id: str, dataset_id: str) -> dict:
  """读取数据集元数据（用于 tool 描述上下文）。"""
  ds_dir = load_dataset_dir(thread_id, dataset_id)
  meta_path = ds_dir / 'meta.json'
  if meta_path.is_file():
    try:
      return json.loads(meta_path.read_text(encoding='utf-8'))
    except (json.JSONDecodeError, OSError):
      pass
  return {}
