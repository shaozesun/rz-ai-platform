"""通道通用：把 Agent 回复里的 Web 专属交付物标签降级为 IM 可展示文本。

1. `<file>` 产物 → 签名下载链接（按 filename 反查会话目录里真实产物，**不信任**标签
   url 里的 dataset_id —— LLM 可能编造）。
2. records 交互表格 → 读整理后 CSV 前几行转纯文本（IM 里没有前端表格组件）。
3. `<chart>` / `<mermaid>` → 删除（IM 不渲染，不留占位）。

各通道传入自己的产物端点前缀与 token 命名空间，其余逻辑完全共用。
"""

import re

from service.channels.artifact_token import create_download_token

_FILE_TAG_RE = re.compile(r'<file\b([^>]*)/>')
_FILE_URL_RE = re.compile(r'url="([^"]*)"')
_FILE_NAME_RE = re.compile(r'name="([^"]*)"')
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


def rewrite_for_channel(
  text: str, thread_id: str, *, url_prefix: str, token_prefix: str, token_ttl: int,
) -> str:
  """把 Agent 回复里的交付物标签降级为 IM 文本。

  `url_prefix` 例如 `{PUBLIC_BASE_URL}/api/v1/wechat/artifacts`；
  `token_prefix` / `token_ttl` 用于签发该通道的产物下载 token。
  """
  base = url_prefix.rstrip('/')

  def _fix(m: re.Match) -> str:
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

    # 普通产物：按文件名反查真实文件 → 签名下载链接
    from core.sandbox.datasets import find_artifact_dir
    ds_dir = find_artifact_dir(thread_id, filename)
    if ds_dir is None:
      return m.group(0)
    token = create_download_token(
      thread_id, ds_dir.name, filename, prefix=token_prefix, ttl=token_ttl,
    )
    return f'[{filename}]({base}/{token})'

  text = _FILE_TAG_RE.sub(_fix, text)
  text = _CHART_TAG_RE.sub('', text)
  text = _MERMAID_TAG_RE.sub('', text)
  return text
