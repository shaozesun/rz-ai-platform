"""数据集存储管理 — analysis_load 落盘 / analysis_exec 读取。

目录结构：{AGENT_ANALYSIS_DATA_DIR}/{thread_id}/{dataset_id}/
  - data.csv    全量记录（沙箱子进程 cwd 读取）
  - meta.json   元数据（来源能力、参数、行数、列名、时间）

按 thread_id（LangGraph 会话）隔离；每会话数据集数量受上限约束，超限删最旧。
"""

import json
import re
import shutil
import time
import uuid
import warnings
from pathlib import Path

from config.settings import settings

# 数据集 id 允许字符（用于生成子目录名，防路径穿越）
_SAFE_RE = re.compile(r'[^A-Za-z0-9_.-]+')

# 列角色识别（供 analysis_load 提示"本数据集能算哪些分析形状"）：
# 噪音列（id/电话/头像/链接）既不是有效维度也不是度量——留着会给出
# 「数值分布（ticketId 分箱）」「散点(ticketId × ticketContentId)」这类垃圾建议。
# 按**词**匹配而不是后缀匹配：ticketId / approveProcessId 也要能识别出来。
_CAMEL_RE = re.compile(r'[A-Z]?[a-z0-9]+|[A-Z]+(?![a-z])|[\u4e00-\u9fff]+')
_ID_WORDS = {
  'id', 'uid', 'uuid', 'guid', 'phone', 'mobile', 'tel', 'avatar',
  'img', 'image', 'photo', 'headimg', 'url', 'link', 'path', 'sn',
}
# 时间列候选：datetime dtype 之外，靠列名再捞一把（如 inCompanyTime / 入职时间 / createTime）。
# `ts` / `report_ts` 是**本项目时序库的列名约定**（DCIM 宽表的时间戳列），
# 不加的话它们会被判成「数值列」→ `analysis_shapes` 就不会提示"时间趋势"
# （其它平台的时间列多是 startTime/createTime，本来就匹配得上，加 ts 不会误伤）。
_TIME_LIKE = re.compile(r'(time|date|month|year|ts|日期|时间|年份)$', re.I)
# 分类列去重数上限：超过就是自由文本（描述/备注），拿来当维度没意义
_MAX_CATEGORY_UNIQUE = 50
# 整数且去重数 ≤ 此值 → 当枚举/标志列（status/specialty/stage/isRead…）而不是度量
_MAX_FLAG_UNIQUE = 10


def _name_words(name: str) -> list[str]:
  """列名切成小写词：camelCase / snake_case / 中文都能切（inCompanyTime → in,company,time）。"""
  return [w.lower() for w in _CAMEL_RE.findall(name)]

# 预览单元格最大字符数（只回摘要，不回原始全量）
_PREVIEW_CELL_CHARS = 80
_PREVIEW_ROWS = 5


class DatasetError(Exception):
  """数据集不存在或非法。"""


def thread_dir(thread_id: str) -> Path:
  """会话目录：DATA_DIR/thread_id（thread_id 做安全化，防路径穿越）。

  沙箱数据集、上传文件、历史归档都落在这个目录下，按会话隔离。
  """
  safe = _SAFE_RE.sub('_', thread_id) or 'default'
  return Path(settings.AGENT_ANALYSIS_DATA_DIR) / safe


def _session_dir(thread_id: str) -> Path:
  """（旧名）会话目录，等价 thread_dir。"""
  return thread_dir(thread_id)


def detail_cache_path(thread_id: str, dataset_id: str) -> Path:
  """工单详情缓存文件：`{会话目录}/detail_cache/{dataset_id}.json`。

  **放在数据集目录之外**：产物下载端点只服务 `{thread_id}/{dataset_id}/`，
  缓存放进去会被当成可下载产物。按数据集名一一对应，淘汰数据集时连带删除。
  """
  safe = _SAFE_RE.sub('_', dataset_id) or 'default'
  return thread_dir(thread_id) / 'detail_cache' / f'{safe}.json'


def _evict_if_needed(thread_dir: Path, dataset_id: str) -> None:
  """每会话数据集数超上限时删最旧（保留刚写入的当前数据集）。

  **只淘汰真正的数据集**——判据是目录里有 `meta.json`（`save_dataset` 写的那份）。
  为什么必须这么判：会话目录下与数据集**同层**还住着别的东西
  （`agent-video/`、`uploads/`、`task_history/`、`workspace/`），老实现把它们都当候选：

  - 受害者是**含文件的目录**（实测 `uploads/`）→ 里面的文件被 `unlink()` **静默删掉**：
    用户上传的 PPT 凭空消失，之后建视频任务只会报"未找到文件"（真实事故）
  - 受害者是 `agent-video/`（子项是目录）→ `unlink()` 抛 `IsADirectoryError`，
    把整个 analysis_load 打断
  - `workspace/`（`analysis_write_file` 的成文交付落点）同理会丢已交付的报告

  触发条件不苛刻：会话目录下子目录数超过 `AGENT_ANALYSIS_MAX_DATASETS_PER_SESSION`
  （默认 20）就会开始淘汰，重度用分析的会话就会踩到。
  """
  max_count = max(1, int(settings.AGENT_ANALYSIS_MAX_DATASETS_PER_SESSION))
  if not thread_dir.is_dir():
    return
  entries = sorted(
    (p for p in thread_dir.iterdir() if (p / 'meta.json').is_file()),
    key=lambda p: p.stat().st_mtime,
    reverse=True,
  )
  # 当前数据集永远保留；其余按新旧留 max_count-1 个
  others = [p for p in entries if p.name != dataset_id]
  for p in others[max_count - 1:]:
    shutil.rmtree(p, ignore_errors=True)     # 目录用 rmtree（老实现的 unlink 在目录上必炸）
    # 详情缓存与数据集同生共死：留在盘上会既无主又无界（月报复用是后续需求）
    (thread_dir / 'detail_cache' / f'{p.name}.json').unlink(missing_ok=True)


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

  # dtypes 与预览都按**读回后**的类型给：沙箱里 analysis_exec 是 pd.read_csv 读回来的，
  # 会把 "1"/"2" 这类编码列重新推断成 int64。若按原始 records 给（全是 str），模型会
  # 照着写 {'1': ...} 的字符串 key，跑到沙箱里匹配不上 int，整列变空。
  try:
    rt = pd.read_csv(ds_dir / 'data.csv')
    dtypes = {c: str(t) for c, t in rt.dtypes.items()}
    preview = _build_preview(rt.to_dict('records'))
    roles = _column_roles(rt)
  except Exception:
    dtypes = {c: str(t) for c, t in df.dtypes.items()}
    preview = _build_preview(records)
    roles = _column_roles(df)

  meta = {
    'dataset_id': dataset_id,
    'capability_id': capability_id,
    'params': params or {},
    'rows': len(records),
    'columns': list(df.columns) if not df.empty else [],
    'dtypes': dtypes,
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
    'dtypes': dtypes,
    'preview': preview,
    'column_roles': roles,
  }


def _json_native(v):
  """numpy 标量 → 原生类型；NaN → None。预览要保持数字类型（不能 str 掉），
  同时保证 json.dumps 不炸（numpy.int64 不可序列化）。"""
  if v is None:
    return None
  item = v.item() if hasattr(v, 'item') else v
  if isinstance(item, float) and item != item:  # NaN
    return None
  return item


def _build_preview(records: list[dict]) -> list[dict]:
  """前 N 行摘要，单元格截断——只回摘要，不进 LLM 上下文。

  非字符串值保持原始类型（数字就是数字，不 str 掉）：模型据此判断字段是 1 还是 "1"，
  直接决定它写映射时 key 用整数还是字符串——类型看不出来就会整列 map 成空。
  """
  preview = []
  for rec in records[:_PREVIEW_ROWS]:
    row = {}
    for k, v in rec.items():
      if isinstance(v, str):
        row[k] = v[:_PREVIEW_CELL_CHARS] + ('…' if len(v) > _PREVIEW_CELL_CHARS else '')
      else:
        row[k] = _json_native(v)
    preview.append(row)
  return preview


def _column_roles(df) -> dict:
  """把列按角色分成 时间列 / 数值列 / 分类列。

  目的：模型只会对分类列做 value_counts（于是只能出柱/条/饼），是因为它手上只有
  「类别+计数」这一种形状。把可用的时间列/数值列点名出来，它才有可能算出
  趋势 / 分布 / 散点 / 交叉。纯事实计算，不生成文案（文案在 analysis_tools 里渲染）。
  """
  import pandas as pd

  roles: dict = {'time': [], 'numeric': [], 'category': []}
  if df is None or getattr(df, 'empty', True):
    return roles
  rows = max(1, len(df))
  for col in df.columns:
    name = str(col)
    if any(w in _ID_WORDS for w in _name_words(name)):
      continue
    s = df[col]
    try:
      filled = float(s.notna().mean())
      if filled <= 0:
        continue  # 全空列（内部字段常见）当维度/度量都没意义
      uniq = int(s.nunique(dropna=True))
      if pd.api.types.is_datetime64_any_dtype(s):
        roles['time'].append(name)
        continue
      if filled >= 0.8 and _TIME_LIKE.search(name) and _parse_dates(s).notna().mean() >= 0.8:
        roles['time'].append(name)   # 中文日期（2012年12月12日）也算
        continue
      if pd.api.types.is_numeric_dtype(s) and filled >= 0.5:
        # 整数且去重数很少 → 枚举/标志列（status/specialty/isRead…），当维度不当度量；
        # 否则才是真度量（耗时/金额/数量…），可出分布/关系图。稀疏列不做度量。
        if _is_flag_like(s) and 0 < uniq <= _MAX_FLAG_UNIQUE:
          roles['category'].append(name)
        else:
          roles['numeric'].append(name)
        continue
      # 分类列：去重数别逼近行数——那说明是姓名/工单号这类标识，不是可聚合的维度
      if 0 < uniq <= _MAX_CATEGORY_UNIQUE and uniq <= max(2, rows * 0.8):
        roles['category'].append(name)
    except Exception:
      continue  # 单列判断失败不影响其它列
  return roles


def _is_flag_like(s) -> bool:
  """整数值列（枚举码/0-1 标志），而非连续度量。"""
  try:
    vals = s.dropna()
    if vals.empty:
      return False
    return bool((vals == vals.astype('int64')).all())
  except Exception:
    return False


def _parse_dates(s):
  """尽力把列解析成日期（中文「2012年12月12日」也认）。

  pandas 不认中文日期写法（实测 format='mixed'/默认都 0% 成功），先归一成 2012-12-12
  再解析——平台数据里 入职时间/发生时间 这类中文字段很常见。
  """
  import pandas as pd

  with warnings.catch_warnings():
    warnings.simplefilter('ignore')  # 逐元素解析会 warn，属噪音日志
    # 注意：pandas 3 里字符串列的 dtype 是 `str` 而不是 `object`，别写死等号判断
    if pd.api.types.is_string_dtype(s) or s.dtype == object:
      s = (
        s.astype(str)
        .str.replace('年', '-', regex=False)
        .str.replace('月', '-', regex=False)
        .str.replace('日', '', regex=False)
      )
    try:
      return pd.to_datetime(s, errors='coerce', format='mixed')
    except (TypeError, ValueError):
      return pd.to_datetime(s, errors='coerce')


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
