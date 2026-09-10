"""飞书产物下载：签名 token + <file> 标签改写为可点 markdown 链接。

Agent 回复里的 `<file url name/>` 在飞书卡片里是纯文本，且原下载端点需要 JWT，
飞书浏览器侧没有登录态。这里为真实存在的产物生成签名 token，把标签改写为公开
端点 markdown 链接（token 即授权，TTL 内可多次下载）。
"""

import json
import logging
import re
import secrets
from typing import Optional

from config.redis_conn import redis_manager
from config.settings import settings

logger = logging.getLogger(__name__)

_TOKEN_KEY = 'lark:artifact:'

# 与 service/agent/streaming.py 保持一致的 <file> 标签解析（避免反向依赖）
_FILE_TAG_RE = re.compile(r'<file\b([^>]*)/>')
_FILE_URL_RE = re.compile(r'url="([^"]*)"')
_FILE_NAME_RE = re.compile(r'name="([^"]*)"')


def create_download_token(thread_id: str, dataset_id: str, filename: str) -> str:
  """为产物生成签名 token 并写入 Redis，返回 token。"""
  token = secrets.token_urlsafe(24)
  payload = json.dumps(
    {'thread_id': thread_id, 'dataset_id': dataset_id, 'filename': filename},
  )
  redis_manager.client.set(
    redis_manager.key(f'{_TOKEN_KEY}{token}'),
    payload,
    ex=settings.LARK_ARTIFACT_TOKEN_TTL,
  )
  return token


def resolve_download_token(token: str) -> Optional[dict]:
  """解析下载 token，返回 {thread_id, dataset_id, filename}；无效返回 None。"""
  raw = redis_manager.client.get(redis_manager.key(f'{_TOKEN_KEY}{token}'))
  if not raw:
    return None
  try:
    return json.loads(raw)
  except json.JSONDecodeError:
    return None


def rewrite_file_tags(text: str, thread_id: str) -> str:
  """把 <file> 标签改写为可点 markdown 下载链接；找不到真实文件则原样保留。

  不信任标签 url 里的 dataset_id（LLM 可能编造），按 filename 反查会话目录里
  真实产物后生成 token。
  """
  from core.sandbox.datasets import find_artifact_dir

  base = settings.PUBLIC_BASE_URL.rstrip('/')

  def _fix(m: re.Match) -> str:
    attrs = m.group(1)
    name_m = _FILE_NAME_RE.search(attrs)
    if not name_m:
      return m.group(0)
    filename = name_m.group(1)
    ds_dir = find_artifact_dir(thread_id, filename)
    if ds_dir is None:
      return m.group(0)
    token = create_download_token(thread_id, ds_dir.name, filename)
    url = f'{base}/api/v1/feishu/artifacts/{token}'
    return f'[{filename}]({url})'

  return _FILE_TAG_RE.sub(_fix, text)


_CHART_TAG_RE = re.compile(r'<chart\b([^>]*)>.*?</chart>', re.DOTALL)
_MERMAID_TAG_RE = re.compile(r'<mermaid\b[^>]*>.*?</mermaid>', re.DOTALL)
_RECORDS_DS_RE = re.compile(r'/agent/artifacts/[^/]+/([^/]+)/records')

_RECORDS_MAX_ROWS = 5
_RECORDS_MAX_COLS = 5


def _records_to_text(thread_id: str, dataset_id: str) -> str:
  """读 records 数据集 view.csv（优先）/data.csv 前几行，转纯文本；失败返回空串。"""
  try:
    from core.sandbox.datasets import load_dataset_dir
    ds_dir = load_dataset_dir(thread_id, dataset_id)
  except Exception:
    return ''

  csv_path = None
  for name in ('view.csv', 'data.csv'):
    p = ds_dir / name
    if p.is_file():
      csv_path = p
      break
  if csv_path is None:
    return ''

  try:
    import pandas as pd
    df = pd.read_csv(csv_path)
  except Exception:
    return ''

  if df.empty:
    return ''

  total = len(df)
  df = df.head(_RECORDS_MAX_ROWS).iloc[:, :_RECORDS_MAX_COLS]
  header = ' | '.join(str(c) for c in df.columns)
  rows = [
    ' | '.join('' if pd.isna(v) else str(v) for v in row)
    for row in df.itertuples(index=False, name=None)
  ]
  lines = [header, *rows]
  if total > _RECORDS_MAX_ROWS:
    lines.append(f'…（共 {total} 行，仅展示前 {_RECORDS_MAX_ROWS} 行）')
  return '\n'.join(lines)


def _file_tag_to_feishu(m: re.Match, thread_id: str, base: str) -> str:
  """<file> 标签 → 下载链接；records 表格额外读 CSV 前几行转文字。"""
  attrs = m.group(1)
  name_m = _FILE_NAME_RE.search(attrs)
  if not name_m:
    return m.group(0)
  filename = name_m.group(1)

  # records 表格：name 是「能力名列表」非真实文件，从 url 解析 dataset_id 读 CSV 转文字
  url_m = _FILE_URL_RE.search(attrs)
  raw_url = url_m.group(1) if url_m else ''
  ds_m = _RECORDS_DS_RE.search(raw_url)
  if ds_m:
    return _records_to_text(thread_id, ds_m.group(1))

  # 普通产物：按文件名反查真实文件 → 下载链接
  from core.sandbox.datasets import find_artifact_dir
  ds_dir = find_artifact_dir(thread_id, filename)
  if ds_dir is None:
    return m.group(0)
  token = create_download_token(thread_id, ds_dir.name, filename)
  download_url = f'{base}/api/v1/feishu/artifacts/{token}'
  return f'[{filename}]({download_url})'


def rewrite_for_feishu(text: str, thread_id: str) -> str:
  """把 Agent 回复里的 Web 专属交付物标签降级为飞书可展示文本。

  1. <file> 产物 → 下载链接；records 表格额外读 CSV 前几行转纯文本。
  2. <chart> 图表 → 删除（飞书无法渲染，不显示占位）。
  3. <mermaid> 流程图 → 删除（同上）。
  """
  base = settings.PUBLIC_BASE_URL.rstrip('/')

  text = _FILE_TAG_RE.sub(lambda m: _file_tag_to_feishu(m, thread_id, base), text)
  text = _CHART_TAG_RE.sub('', text)
  text = _MERMAID_TAG_RE.sub('', text)
  return text
