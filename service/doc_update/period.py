"""期号解析与目录形态判定（纯函数，不连库不联网）。

**默认只从目录名解析期号**：文件名里的日期是坑（如 `…2026-11更新.xlsx`），
会让「原地更新」的文档每月都被当成新一期。例外：`period_only` 项（能源月报）
各部门把期号写在文件名里、目录只到年份，允许 `name_period` 兜底；即便在例外分支
也**不使用 `create_time` 的年份**（分部常跨年补传，会把 2025 期算成 2026）。

两种目录形态（由 `detect_layout` 自动判定）：
- `period` 按期提交型：路径里有月份目录（`2026/9月`、`2026-09`、`2026年9月`）——
  每月新起一份文件，只需看「本期应到那一期有没有文件」，不下载、不哈希。
- `flat` 原地更新型：没有月份目录（整定表/阈值表这类一份文件长期修改）——
  走内容哈希，与上一期快照比。
"""

import re
from datetime import datetime

LAYOUT_PERIOD = 'period'
LAYOUT_FLAT = 'flat'

_YEAR_MONTH = re.compile(r'^(\d{4})\s*[-_/年.]\s*(\d{1,2})\s*月?(?:份)?$')
_YEAR_ONLY = re.compile(r'^(\d{4})\s*年?$')
_MONTH_ONLY = re.compile(r'^(\d{1,2})\s*月(?:份)?$')
# 目录名带业务标题时月份夹在中间，如「润泽国际信息港A-1能源报告【2025年10月总第116期】」。
# 只认「YYYY年M月」这种无歧义写法；`2026-11更新` 一类（文件名坑）不做模糊匹配。
_EMBEDDED_YEAR_MONTH = re.compile(r'(\d{4})\s*年\s*(\d{1,2})\s*月')

# 文件名里的期号写法（实测 21 个部门）。按顺序取第一个有合法结果的写法。
_NAME_FULL_DATE = re.compile(r'(20\d{2})(\d{2})(\d{2})(?!\d)')       # 【20261031】
_NAME_YEAR_MONTH = re.compile(r'(\d{4})\s*年\s*(\d{1,2})\s*月')        # 2026年10月
_NAME_DASH_MONTH = re.compile(r'(20\d{2})\s*[-_/.]\s*(\d{1,2})(?!\d)')  # -2026-10月
_NAME_YYYYMM = re.compile(r'(?<!\d)(20\d{2})(0[1-9]|1[0-2])(?!\d)')   # 【202610】
_NAME_MONTH = re.compile(r'(?<!\d)(\d{1,2})\s*月份?(?!度)')             # 纯 `-10月`、4月份
_NAME_YEAR = re.compile(r'(20\d{2})\s*年')
_SEG_YEAR = re.compile(r'(?<!\d)(?:19|20|21)\d{2}(?!\d)')             # 目录里的年份


def _norm(text) -> str:
  return str(text or '').replace('\u3000', ' ').strip()


def _mk(year: int, month: int) -> str:
  if 1 <= month <= 12 and 2000 <= year <= 2100:
    return f'{year:04d}-{month:02d}'
  return ''


def parse_period(segments, fallback_year: int | None = None) -> str:
  """从目录名序列里解析月份级期号 `'YYYY-MM'`（取最深层的那一个）；解析不到返回 `''`。

  年份段向上找最近的四位年份；没有年份时用 `fallback_year`（一般取文件创建时间的年份）。
  """
  year = None
  found = ''
  for raw in segments or []:
    seg = _norm(raw)
    if not seg:
      continue
    m = _YEAR_MONTH.match(seg)
    if m:
      year = int(m.group(1))
      found = _mk(year, int(m.group(2))) or found
      continue
    m = _YEAR_ONLY.match(seg)
    if m:
      year = int(m.group(1))
      continue
    m = _MONTH_ONLY.match(seg)
    if m:
      use_year = year if year else fallback_year
      if use_year:
        found = _mk(int(use_year), int(m.group(1))) or found
      continue
    m = _EMBEDDED_YEAR_MONTH.search(seg)
    if m:
      year = int(m.group(1))
      found = _mk(year, int(m.group(2))) or found
  return found


def period_of_file(file: dict, from_name: bool = False) -> str:
  """单个文件（walk 产出的 dict：`segments` + `topic` + `create_time`）的期号。

  目录名能解析出月份级期号时**以目录为准**；`from_name=True` 且目录解析不到时，
  退到文件名（`period_only` 项用），年份取目录里的年份，**不用 `create_time`**。
  """
  segments = file.get('segments') or []
  found = parse_period(segments, _create_year(file.get('create_time')))
  if found or not from_name:
    return found
  return name_period(file.get('topic') or '', _segments_year(segments))


def name_period(topic, fallback_year: int | None = None) -> str:
  """从文件名解析月份级期号；解析不到返回 `''`。

  优先级：8 位日期 → `YYYY年M月` → `YYYY-M` → `YYYYMM` → 纯 `M月`。
  纯月份没有自带年份时，取该月**之前最近的 `YYYY年`**（没有则取文中任意一个），
  再没有才用 `fallback_year`（一般传目录里的年份）。
  """
  text = _norm(topic)
  if not text:
    return ''
  for regex in (_NAME_FULL_DATE, _NAME_YEAR_MONTH, _NAME_DASH_MONTH, _NAME_YYYYMM):
    value = _last_period(regex, text)
    if value:
      return value
  for m in reversed(list(_NAME_MONTH.finditer(text))):
    year = _year_near(text, m.start(), fallback_year)
    value = _mk(int(year), int(m.group(1))) if year else ''
    if value:
      return value
  return ''


def _last_period(regex, text: str) -> str:
  """取最后一个合法命中（同一文件名里常有旧期号在括号里）。"""
  for m in reversed(list(regex.finditer(text))):
    value = _mk(int(m.group(1)), int(m.group(2)))
    if value:
      return value
  return ''


def _year_near(text: str, pos: int, fallback_year: int | None) -> int | None:
  years = [(m.start(), int(m.group(1))) for m in _NAME_YEAR.finditer(text)]
  before = [year for start, year in years if start < pos]
  if before:
    return before[-1]
  after = [year for start, year in years if start > pos]
  if after:
    return after[0]
  return fallback_year


def _segments_year(segments) -> int | None:
  """目录序列里最后一个四位年份（`2026`、`2026年`、`2026年输出物` 都认）。"""
  year = None
  for raw in segments or []:
    for m in _SEG_YEAR.finditer(_norm(raw)):
      year = int(m.group(0))
  return year


def detect_layout(files) -> str:
  """只要有一条路径解析出月份级期号，就按「按期提交型」判。"""
  for f in files or []:
    if period_of_file(f):
      return LAYOUT_PERIOD
  return LAYOUT_FLAT


def pick_latest_period(files, from_name: bool = False) -> str:
  periods = [period_of_file(f, from_name) for f in files or []]
  periods = [p for p in periods if p]
  return max(periods) if periods else ''


def expected_period(now: datetime | None = None, grace_months: int = 1) -> str:
  """本期应到：当前月回退 `grace_months` 月（默认 1，即上月）。

  这样两种命名习惯都能过：按报告期（10 月报叫 `2026-09`）与按交付月（叫 `2026-10`）。
  """
  now = now or datetime.now()
  total = now.year * 12 + (now.month - 1) - max(0, int(grace_months))
  return f'{total // 12:04d}-{total % 12 + 1:02d}'


def _create_year(value) -> int | None:
  """`createTime` 可能是毫秒/秒时间戳或 ISO 串；只取年份，取不到返回 None。"""
  try:
    if isinstance(value, (int, float)):
      n = float(value)
      if n > 1e12:
        n /= 1000
      if n <= 0:
        return None
      return datetime.fromtimestamp(n).year
    text = _norm(value)
    if not text:
      return None
    m = re.match(r'^(\d{4})[-/年]', text)
    return int(m.group(1)) if m else None
  except (OSError, ValueError, OverflowError):
    return None
