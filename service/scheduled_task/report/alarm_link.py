"""事件报告：重复类目的关联告警（告警值 + 四档阈值）。

**为什么只能启发式匹配**：事件单（列表 + 详情）没有任何设备 / 点位 / guid 字段，
能用的只有「发生时间 + 园区 + 部门 + 事件名称」；而 DCIM 告警是按测点推的。
所以这里不追求「匹配上」，而是**不配优于配错**：时间窗先缩到 ±2 分钟，
再用机房编码收敛，多候选交给模型仲裁，判不出就留空。

口径（用户 2026-10-30 确认，2026-11-02 改为全量 + 逐次发生）：

- **全部重复类目都关联**（`stats['repeat']`，同一 `projectName` ≥2 次），不设类目数上限；
- **每一次发生各搜一次**（代表单先搜；其余发生先复用代表单命中的点位签名，
  没有签名时再按时间窗检索）；
- 时间窗内命中多条时**由模型按地点 / 设备 / 测点 / 事件名称语义仲裁**，
  **不要求字面完全相同**；判不出留空；每次歧义发生各自一次仲裁调用；
- 展示**告警值**与该点位**四档阈值**（提示 / 一般 / 重要 / 紧急，未配置印 `—`）；
  **关联上的告警全部展示、不截断**（用户 2026-11-02 要求）。
- 判定结果按 `(工单号, 发生时间, 口径)` 落判定表（`alarm_decision.py`），**一行 = 一次发生**：
  已经判过的发生（含 `not_covered` / `no_alarm`）直接复用，不再查库、不再调模型；
  对话工具（`tools/event_alarm_tools.py`）与报告共用同一张表。

数据边界：

- DCIM 告警当前只覆盖**廊坊**园区（`A` / `B1M1` / `B1M2` 集群），且自
  `2026-10-29` 起才有数据 —— 更早的期次、其他园区一律关联不到，只出覆盖说明；
- **库里的 `cluster` 是带园区前缀的代号**（`廊坊A` / `廊坊B1M1` / `廊坊B1M2`），
  按短码查 SQL 恒不命中（2026-11-02 生产实测：候选数全为 0）；
- 全国口径的 `companyName` 是**法人主体**，要用 mgmt 公司档案的 `gardenName`
  才能判园区（园区级报告直接以预设的 `park` 为准）；
- 当前态 `dcim_alarms.alarm_value` 会被刷新(2)/恢复(3)推送覆盖 →
  **触发值回 `dcim_alarms_history` 取第一条 `status='1'`**，取不到才回退当前态并标注。

失败只记日志：整轮异常返回 `available=False`，**绝不让报告失败**。
纯服务端逻辑，不依赖 Agent；单测可注入 `repo_factory` / `picker`。
"""

from __future__ import annotations

import json
import logging
import re
import time
import unicodedata
from datetime import date, datetime

from config.settings import settings
from service.scheduled_task.report import alarm_decision

logger = logging.getLogger(__name__)

ALARM_COVERAGE_PARKS = ('廊坊',)
ALARM_COVERAGE_CLUSTER_SUFFIXES = ('A', 'B1M1', 'B1M2')
# 库里的 `cluster` 是**带园区前缀**的代号（实测 `廊坊A` / `廊坊B1M1` / `廊坊B1M2`）。
# 传短码（`A`）SQL 恒不命中 —— 2026-11-02 生产实测：全国/园区报告候选数均为 0。
ALARM_COVERAGE_CLUSTERS = tuple('廊坊' + s for s in ALARM_COVERAGE_CLUSTER_SUFFIXES)
ALARM_COVERAGE_START = date(2026, 10, 29)
TIME_WINDOW_SECONDS = 120
LLM_MAX_CANDS = 8            # 交给模型仲裁的候选上限（按文本 Dice 降序）
LLM_TIMEOUT = 15
ALARM_QUERY_LIMIT = 200

NOTE = ('未关联到告警不等于没有告警：DCIM 告警当前仅覆盖廊坊园区'
        '（A / B1M1 / B1M2 集群），且自 2026-10-29 起才有数据。')

_LEVEL_LABELS = {1: '提示', 2: '一般', 3: '重要', 4: '紧急'}
_LEVEL_RANK = {'紧急': 4, '重要': 3, '一般': 2, '提示': 1}
# 4 档阈值字段（轻 → 重）；与 `service/query/dcim_meta/client.py` 的 `_ALARM_COLUMNS` 同源
_TIERS = (('提示', 'hint_alarm'), ('一般', 'general_alarm'),
          ('重要', 'important_alarm'), ('紧急', 'urgent_alarm'))
_ROOM_RE = re.compile(r'[A-Z]\d+(?:M\d+)?')

_MAX_TEXT = 200
_MAX_REASON = 60
_MAX_TOKENS = 300

_SYSTEM = (
  '你是数据中心事件与动环告警的关联助手。用户会给出一条事件工单与若干条候选告警，'
  '请判断这条事件对应哪一条告警。\n'
  '判断依据：地点（园区 / 楼宇 / 机房编码）、设备与测点、事件名称语义、发生时间接近程度。'
  '事件名称与告警内容不要求字面完全相同（例如「UPS 市电中断」与「A1 UPS 输出停电」可对应）。\n'
  '只能从候选中选，不得臆造；证据不足或无法判断时 pick 必须为 null。\n'
  '只输出 JSON：{"pick": <候选序号或 null>, "reason": "<不超过60字>"}'
)
_PICK_RE = re.compile(r'"pick"\s*:\s*(null|-?\d+)', re.I)
_FENCE_RE = re.compile(r'^```[a-zA-Z]*\s*|\s*```$')


# ── 基础助手 ──
def _num(value) -> float | None:
  try:
    return float(value)
  except (TypeError, ValueError):
    return None


def _to_seconds(value) -> int | None:
  """`occurrenceTime` 是毫秒 epoch（也兼容秒级 / 数字字符串）。"""
  num = _num(value)
  if num is None or num <= 0:
    return None
  if num > 1e11:                 # 毫秒
    num /= 1000
  return int(num)


def _fmt_time(ts) -> str:
  try:
    return time.strftime('%Y-%m-%d %H:%M', time.localtime(int(ts)))
  except (TypeError, ValueError, OSError):
    return ''


def _as_date(value) -> date | None:
  if isinstance(value, datetime):
    return value.date()
  if isinstance(value, date):
    return value
  text = str(value or '')[:10]
  try:
    return date.fromisoformat(text)
  except ValueError:
    return None


def _norm(text) -> str:
  """NFKC + 小写 + 去空白与标点（中文 bigram 相似度的前置归一）。"""
  s = unicodedata.normalize('NFKC', str(text or '')).lower()
  return ''.join(ch for ch in s if unicodedata.category(ch)[0] not in 'PZS')


def _bigrams(text) -> set[str]:
  s = _norm(text)
  if len(s) < 2:
    return {s} if s else set()
  return {s[i:i + 2] for i in range(len(s) - 1)}


def _dice(a, b) -> float:
  ga, gb = _bigrams(a), _bigrams(b)
  if not ga or not gb:
    return 0.0
  return 2 * len(ga & gb) / (len(ga) + len(gb))


def _room_codes(*texts) -> list[str]:
  """从事件文本里抽机房码（如 `A1` / `B1M2`）；抽不到返回空列表。"""
  codes: list[str] = []
  for text in texts:
    for code in _ROOM_RE.findall(str(text or '').upper()):
      if code not in codes:
        codes.append(code)
  return codes


# ── 阈值（与 `service/query/dcim_meta/client.py` 的 `_enabled` / `_breach` 同一语义）──
def _enabled(raw) -> bool | None:
  """`maxUse`/`minUse` 这类开关字段 → True/False；缺失返回 None。"""
  if raw is None or (isinstance(raw, str) and not raw.strip()):
    return None
  try:
    return float(raw) != 0
  except (TypeError, ValueError):
    return None


def _side(raw: dict, use_key: str, alarm_key: str) -> float | None:
  """一档阈值的单边：开关关掉（或值为 0 且未配）→ None。

  ⚠️ **判据必须用 `maxUse`/`minUse`**，不能看 `maxAlarm` 是否为 0 ——
  通断类测点 `maxUse:1, maxAlarm=0` 的 0 是真上限。唯一来源见上方链接。
  """
  if alarm_key not in raw:
    return None
  bound = _num(raw.get(alarm_key))
  if bound is None:
    return None
  flag = _enabled(raw.get(use_key))
  if flag is None:               # 开关字段缺失 → 退回「数值非零才算」
    flag = bound != 0
  return bound if flag else None


def bounds_of(raw) -> list | None:
  """一个阈值 JSONB 对象 → `[下限, 上限]`；整档未配置返回 None（渲染为 `—`）。"""
  if not isinstance(raw, dict):
    return None
  lo = _side(raw, 'minUse', 'minAlarm')
  hi = _side(raw, 'maxUse', 'maxAlarm')
  if lo is None and hi is None:
    return None
  return [lo, hi]


def _breach(value: float | None, raw) -> tuple[str, float] | None:
  """值是否越界（与 `client._breach` 同语义）。"""
  if value is None or not isinstance(raw, dict):
    return None
  for use_key, alarm_key, direction, cmp in (
    ('maxUse', 'maxAlarm', '高于上限', lambda v, t: v > t),
    ('minUse', 'minAlarm', '低于下限', lambda v, t: v < t),
  ):
    if alarm_key not in raw:
      continue
    bound = _num(raw.get(alarm_key))
    if bound is None:
      continue
    flag = _enabled(raw.get(use_key))
    if flag is None:
      flag = bound != 0
    if flag and cmp(value, bound):
      return direction, bound
  return None


def format_trigger(bounds) -> str:
  """`[lo, hi]` → **告警触发条件**文本：`低于 10 或 高于 30` / `高于 30` / `低于 10`。

  这是「什么情况会告警」，不是正常取值范围 —— `maxUse`/`minUse` 是开关，
  `maxAlarm`/`minAlarm` 是触发点。未配置 → `—`。
  """
  if not bounds:
    return '—'
  lo, hi = bounds
  if lo is not None and hi is not None:
    return f'低于 {_fmt_num(lo)} 或 高于 {_fmt_num(hi)}'
  if hi is not None:
    return f'高于 {_fmt_num(hi)}'
  if lo is not None:
    return f'低于 {_fmt_num(lo)}'
  return '—'


def _fmt_num(value) -> str:
  num = _num(value)
  if num is None:
    return '—'
  return f'{num:g}'


def format_value(value, unit: str = '') -> str:
  """告警值展示：`12.5` / `12.5 ℃`；无值 → `—`。"""
  text = _fmt_num(value)
  return f'{text} {unit}'.strip() if unit and text != '—' else text


def _thresholds(row: dict | None) -> dict:
  return {label: bounds_of((row or {}).get(col)) for label, col in _TIERS}


def _breached_tiers(value: float | None, row: dict | None) -> list[str]:
  """命中的档位，按重 → 轻（紧急 → 提示）排列。"""
  if value is None or not isinstance(row, dict):
    return []
  hits = []
  for label, col in reversed(_TIERS):
    if _breach(value, row.get(col)):
      hits.append(label)
  return hits


# ── 代表单 ──
def _repeat_rows(stats) -> list[dict]:
  return [r for r in ((stats or {}).get('repeat') or []) if isinstance(r, dict)]


def _pick_record(item: dict, cur_recs: list[dict]) -> dict | None:
  """代表单：类目里 `ticketNo` 对应的那条记录；缺票号时退回同类最早发生的一条。"""
  recs = [r for r in (cur_recs or []) if isinstance(r, dict)]
  no = str(item.get('ticketNo') or '').strip()
  if no:
    for rec in recs:
      if str(rec.get('ticketNo') or '').strip() == no:
        return rec
  name = _norm(item.get('name'))
  same = [r for r in recs if _norm(r.get('projectName')) == name
          and _to_seconds(r.get('occurrenceTime'))]
  if same:
    return min(same, key=lambda r: _to_seconds(r.get('occurrenceTime')))
  return None


def _occurrences(item: dict, name: str, cur_recs: list[dict],
                 rep: dict | None) -> list[dict]:
  """该类目的**全部发生**：代表单在前，其余按发生时间升序；按 (票号, 时间) 去重。

  同一张单号可能对应多行（事件列表会重复），也可能是补录 —— 用「票号 + 秒级时间」
  去重；缺发生时间的记录不参与检索（没有时间锚点，搜不出可信候选）。
  """
  def key(rec: dict) -> tuple[str, int | None]:
    return str(rec.get('ticketNo') or '').strip(), _to_seconds(rec.get('occurrenceTime'))

  seen, out = set(), []
  if isinstance(rep, dict):
    seen.add(key(rep))
    out.append(rep)
  # 与 `_repeat_problems` 的计数口径一致：去首尾空白后字面相等（不做标点归一，
  # 否则会把统计成两个类目的名称合到一起，次数与发生数自相矛盾）
  target = str(name or '').strip()
  same = [r for r in (cur_recs or [])
          if isinstance(r, dict)
          and str(r.get('projectName') or '').strip() == target
          and _to_seconds(r.get('occurrenceTime')) is not None]
  same.sort(key=lambda r: _to_seconds(r.get('occurrenceTime')))
  for rec in same:
    k = key(rec)
    if k in seen:
      continue
    seen.add(k)
    out.append(rec)
  return out


def _detail_text(details, ticket_no: str) -> str:
  if not ticket_no:
    return ''
  for row in ((details or {}).get('rows') or []):
    if str(row.get('ticketNo') or '').strip() != ticket_no:
      continue
    fields = row.get('fields') or {}
    return str(fields.get('事件名称') or fields.get('事件描述') or '')[:_MAX_TEXT]
  return ''


def _covered_park(rec: dict | None, park: str | None, *texts,
                  company_parks: dict | None = None) -> bool | None:
  """True 覆盖 / False 明确不覆盖 / None 无法判断（园区字段缺失）。

  **园区级报告以显式 `park` 为准**。全国口径没有单个园区：`companyName` 是法人主体
  （如「润泽科技发展有限公司」），要先经 mgmt 公司档案换成 `gardenName`（「廊坊园区」）
  才能判；取不到档案时再看部门 / 事件名称里的集群代号（`B1M2分部`、`B1M2-301机房…`），
  命中覆盖集群才算 —— 不猜 `A18` 这类跨园区歧义码。
  """
  park_text = str(park or '').strip()
  if park_text and park_text != 'nationwide':
    return any(k in park_text for k in ALARM_COVERAGE_PARKS)
  name = str((rec or {}).get('companyName') or '').strip()
  if any(k in name for k in ALARM_COVERAGE_PARKS):
    return True
  garden = str((company_parks or {}).get(name) or '')
  if garden:
    return any(k in garden for k in ALARM_COVERAGE_PARKS)
  codes = set(_room_codes((rec or {}).get('dc'), *texts))
  if codes & set(ALARM_COVERAGE_CLUSTER_SUFFIXES):
    return True
  if name:
    # 档案可用时才敢判「非廊坊」；档案不可用 → None（unknown，不落表，下次重试）
    return False if company_parks is not None else None
  return None


def _needs_company_map(rows: list[dict], cur_recs: list[dict],
                       park: str | None) -> bool:
  """全国口径且确有类目只能靠法人主体判断时才去查公司档案（园区级不用查）。"""
  park_text = str(park or '').strip()
  if park_text and park_text != 'nationwide':
    return False
  for item in rows:
    rec = _pick_record(item, cur_recs) or {}
    name = str(rec.get('companyName') or '').strip()
    if not name or any(k in name for k in ALARM_COVERAGE_PARKS):
      continue
    codes = set(_room_codes(rec.get('dc'), item.get('name')))
    if not codes & set(ALARM_COVERAGE_CLUSTER_SUFFIXES):
      return True
  return False


async def _company_parks() -> dict[str, str] | None:
  """`companyName` → `gardenName`（法人主体 → 园区）。

  档案不可用返回 **None**（≠ 空表）：调用方据此把园区判不出的发生标成 `unknown`
  （不落表、下次重试），不能写死成「非廊坊」。
  """
  try:
    from service.query.mgmt.http import company_registry

    rows = await company_registry()
  except Exception as exc:  # noqa: BLE001 — 档案不可用只降级，不能让报告失败
    logger.warning('公司档案不可用，无法按法人主体判断园区：%s', exc)
    return None
  return {str(r.get('companyName') or '').strip(): str(r.get('gardenName') or '').strip()
          for r in (rows or []) if str(r.get('companyName') or '').strip()}


# ── 候选告警 ──
def _alarm_text(alarm: dict) -> str:
  return ' '.join(str(alarm.get(k) or '') for k in
                  ('device_name', 'point_name', 'content', 'space_name'))


def _rank_candidates(cands: list[dict], text: str, ts: int) -> list[dict]:
  """Dice 降序；保证时间最近的那条在列（它常常是文本最不像、但时间完全对上的）。"""
  scored = sorted(cands, key=lambda a: (-_dice(text, _alarm_text(a)),
                                        abs(int(a.get('alarm_time') or 0) - ts)))
  top = scored[:LLM_MAX_CANDS]
  if len(scored) > LLM_MAX_CANDS and scored:
    nearest = min(cands, key=lambda a: abs(int(a.get('alarm_time') or 0) - ts))
    key = (nearest.get('cluster'), nearest.get('event_log_id'))
    if not any((a.get('cluster'), a.get('event_log_id')) == key for a in top):
      top[-1] = nearest
  return top


def _narrow_by_room(cands: list[dict], codes: list[str]) -> list[dict]:
  """按机房码收敛；命中 0 条时退回全集（不因抽错码丢掉全部候选）。"""
  if not codes:
    return cands
  kept = [a for a in cands
          if any(code in str(a.get('space_name') or '').upper() for code in codes)]
  return kept or cands


def _dedupe(cands: list[dict]) -> list[dict]:
  out, seen = [], set()
  for a in cands:
    key = (a.get('cluster'), a.get('event_log_id'))
    if key in seen:
      continue
    seen.add(key)
    out.append(a)
  return out


def _sort_alarms(items: list[dict]) -> list[dict]:
  """等级降序（紧急 → 提示），同级按时间升序。"""
  return sorted(items, key=lambda a: (-_LEVEL_RANK.get(a.get('level'), 0),
                                      int(a.get('ts') or 0)))


def _alarm_view(alarm: dict, *, value, value_from: str, unit: str,
                thresholds: dict, breached: list[str]) -> dict:
  level = alarm.get('alarm_level')
  return {
    # 身份键：判定表只存 `(cluster, event_log_id)`，展示字段实时取
    'cluster': str(alarm.get('cluster') or ''),
    'event_log_id': str(alarm.get('event_log_id') or ''),
    'time': _fmt_time(alarm.get('alarm_time')),
    'ts': int(alarm.get('alarm_time') or 0),
    'level': _LEVEL_LABELS.get(level, str(level or '')),
    'device': str(alarm.get('device_name') or ''),
    'point': str(alarm.get('point_name') or ''),
    'content': str(alarm.get('content') or ''),
    'space': str(alarm.get('space_name') or ''),
    'value': value,
    'unit': unit,
    'thresholds': thresholds,
    'breached': breached,
    'value_from': value_from,
  }


def _identity(alarm: dict | None) -> dict | None:
  """告警身份（判定表持久化的唯一定位键）。"""
  if not alarm:
    return None
  cluster = str(alarm.get('cluster') or '')
  event_log_id = str(alarm.get('event_log_id') or '')
  if not cluster or not event_log_id:
    return None
  return {'cluster': cluster, 'event_log_id': event_log_id}


def _snapshot(alarm: dict | None) -> dict | None:
  """告警展示快照：只在告警行被软删、实时查不到时兜底（值/阈值不存）。"""
  if not alarm:
    return None
  level = alarm.get('alarm_level')
  return {
    'ts': int(alarm.get('alarm_time') or 0),
    'level': _LEVEL_LABELS.get(level, str(level or '')),
    'device': str(alarm.get('device_name') or ''),
    'point': str(alarm.get('point_name') or ''),
    'content': str(alarm.get('content') or ''),
    'space': str(alarm.get('space_name') or ''),
  }


def _snapshot_row(key: tuple, snapshot: dict | None) -> dict:
  """判定表快照 → `_enrich_all` 能吃的告警行（缺值/阈值，展示为 `—`）。"""
  snap = snapshot or {}
  return {
    'cluster': key[0], 'event_log_id': key[1],
    'alarm_time': int(snap.get('ts') or 0),
    'alarm_level': snap.get('level') or '',
    'device_name': snap.get('device') or '',
    'point_name': snap.get('point') or '',
    'content': snap.get('content') or '',
    'space_name': snap.get('space') or '',
    'alarm_value': None,
  }


# ── 模型仲裁（一次独立调用，不经 Agent；照 `incident_stats/arbitrate.py` 的形态）──
def build_prompt(event: dict, cands: list[dict]) -> tuple[str, str]:
  head = {
    '事件名称': event.get('name') or '',
    '事件描述': str(event.get('describe') or '')[:_MAX_TEXT],
    '发生时间': _fmt_time(event.get('occurrence_ts')),
    '园区': event.get('park') or '',
    '部门/机房': event.get('dc') or '',
    '等级': event.get('level') or '',
    '专业': event.get('specialty') or '',
  }
  items = []
  for i, cand in enumerate(cands, 1):
    items.append({
      '序号': i,
      '触发时间': _fmt_time(cand.get('alarm_time')),
      '与发生时间相差秒': int(cand.get('delta') or 0),
      '告警值': cand.get('alarm_value'),
      '空间名': cand.get('space_name') or '',
      '设备': cand.get('device_name') or '',
      '测点': cand.get('point_name') or '',
      '告警内容': cand.get('content') or '',
      '级别': _LEVEL_LABELS.get(cand.get('alarm_level'), ''),
    })
  user = json.dumps({'事件': head, '候选告警': items}, ensure_ascii=False, indent=1)
  return _SYSTEM, user


def _loads(raw: str) -> dict:
  text = _FENCE_RE.sub('', (raw or '').strip())
  try:
    out = json.loads(text)
  except (TypeError, ValueError):
    return {}
  return out if isinstance(out, dict) else {}


def parse_pick(raw: str, n: int) -> int | None:
  """模型回复 → 候选序号（1 起）；`null` / 越界 / 垃圾一律 None。"""
  data = _loads(raw)
  value = data.get('pick') if data else None
  if value is None:                       # json 解析失败或无 pick 键 → 正则兜底
    match = _PICK_RE.search(raw or '')
    if not match:
      return None
    if match.group(1).lower() == 'null':
      return None
    value = match.group(1)
  if isinstance(value, bool):
    return None
  try:
    idx = int(value)
  except (TypeError, ValueError):
    return None
  return idx if 1 <= idx <= n else None


async def _default_picker(event: dict, cands: list[dict]) -> tuple[int | None, str]:
  from core.model_gateway import model_gateway

  system, user = build_prompt(event, cands)
  try:
    raw = await model_gateway.chat(
      messages=[{'role': 'system', 'content': system},
                {'role': 'user', 'content': user}],
      temperature=0, max_tokens=_MAX_TOKENS, json_mode=True, timeout=LLM_TIMEOUT,
      caller='event_alarm_link',
    )
  except Exception as e:                  # noqa: BLE001 —— 仲裁失败只留空
    logger.warning('[AlarmLink] 模型仲裁调用失败：%s', str(e)[:200])
    return None, ''
  return parse_pick(raw, len(cands)), str(_loads(raw).get('reason') or '')[:_MAX_REASON]


# ── 取数 ──
def _default_repo():
  from service.dcim_ingest.repository import DcimRepository

  return DcimRepository(settings.DCIM_PG_DSN)


def _query_alarms(repo, ts: int) -> list[dict]:
  """±2 分钟候选，按**触发态时间**（历史表第一条 `status='1'`）。

  当前态 `alarm_time` 会被刷新(2)/恢复(3)推送覆盖，晚到的通报按当前态查会漏
  （事件统计表已改按触发时间，事件报告此前没改）。查询结果把 `alarm_time`
  统一成 `trigger_ts`，下游算 `delta`、展示时间、给模型的「触发时间」都用它。
  """
  rows = repo.find_alarms_by_trigger_time(
    list(ALARM_COVERAGE_CLUSTERS), ts - TIME_WINDOW_SECONDS, ts + TIME_WINDOW_SECONDS,
    limit=ALARM_QUERY_LIMIT,
  )
  out = []
  for row in rows or []:
    item = dict(row)
    trig = item.get('trigger_ts')
    if trig:
      item['alarm_time'] = int(trig)
    item['delta'] = int(item.get('alarm_time') or 0) - ts
    out.append(item)
  return _dedupe(out)


def _point_key(alarm: dict) -> str:
  """`dcim_points.guid` 的口径：`device_guid + '.' + 测点代号`。

  `dcim_alarms.point_guid` 只存测点代号（如 `PDU_I09`），而点位表的主键是
  设备号拼测点代号（如 `CHNLFB01-…-0000M2402002.PDU_I09`）。直接拿前者去查
  `WHERE guid = ANY(...)` 恒 0 行 —— 阈值列全空就是这么来的（2026-11-02 实测：
  按拼接键查 3000/3000 条告警都能命中）。
  """
  point = str(alarm.get('point_guid') or '')
  device = str(alarm.get('device_guid') or '')
  if not point or '.' in point or not device:   # 已是全键 / 缺设备号 → 原样
    return point
  return f'{device}.{point}'


def _signature(alarm: dict) -> tuple | None:
  """点位签名 `(cluster, device_guid, point_guid)`；缺测点号时不可复用。"""
  point = str(alarm.get('point_guid') or '')
  if not point:
    return None
  return (alarm.get('cluster'), str(alarm.get('device_guid') or ''), point)


def _enrich_all(repo, alarms: list[dict]) -> list[dict]:
  """批量补齐告警值（触发时刻优先）+ 四档阈值 + 越限档（两次批量查询）。"""
  alarms = [a for a in alarms if isinstance(a, dict)]
  if not alarms:
    return []
  trig_pairs = list({(a.get('cluster'), a.get('event_log_id')) for a in alarms})
  trigger = repo.find_alarm_trigger_values(trig_pairs) or {}
  pt_pairs = list({(a.get('cluster'), _point_key(a)) for a in alarms if _point_key(a)})
  points = (repo.find_point_thresholds(pt_pairs) or {}) if pt_pairs else {}
  out = []
  for alarm in alarms:
    pair = (alarm.get('cluster'), alarm.get('event_log_id'))
    value, value_from = trigger.get(pair), 'trigger'
    if value is None:
      value = _num(alarm.get('alarm_value'))
      value_from = 'current' if value is not None else 'missing'
    row = points.get((alarm.get('cluster'), _point_key(alarm))) or {}
    out.append(_alarm_view(
      alarm, value=value, value_from=value_from,
      unit=str(row.get('unit') or ''),
      thresholds=_thresholds(row), breached=_breached_tiers(value, row),
    ))
  return out


def _base() -> dict:
  return {'available': False, 'reason': '', 'clusters': [],
          'linked_n': 0, 'llm_calls': 0, 'cache_hits': 0, 'note': NOTE}


def _unmatched(name: str, count, occurrence: dict | None, reason: str, *,
               total: int = 0) -> dict:
  return {'name': name, 'count': count, 'total': total, 'matched_n': 0,
          'occurrence': {**(occurrence or {}), 'match': '', 'reason': reason},
          'occurrences': [{**(occurrence or {}), 'match': '', 'reason': reason}],
          'alarms': []}


def _verdict(status: str, match: str = '', reason: str = '', *,
             alarm: dict | None = None) -> dict:
  """单次发生的判定结果（内部结构；报告与对话共用）。

  `status` 见 `alarm_decision.STATUSES`；另有 `unknown`（档案不可用 / 仲裁异常）与
  `no_time`（缺发生时间）两种**不落表**的临时状态。
  """
  return {
    'status': status,
    'match': match if status == 'matched' else '',
    'reason': reason,
    'alarm': _identity(alarm),
    'alarm_row': alarm,
    'alarm_ts': int(alarm.get('alarm_time') or 0) if alarm else None,
    'alarm_snapshot': _snapshot(alarm),
    'point_sig': _signature(alarm) if alarm else None,
  }


async def _match_occurrence(rec: dict, meta: dict, ts: int | None, name: str,
                            details: dict, park: str | None, company_parks: dict | None,
                            repo_get, picker, out: dict,
                            signature: tuple | None) -> dict:
  """单次发生 → 判定结果 dict；`repo_get` 是惰性连接工厂（判定不需要时不碰 PG）。"""
  covered = _covered_park(rec, park, name, company_parks=company_parks)
  if covered is None:
    return _verdict('unknown', reason='无法确定事件所属园区，未查询 DCIM 告警')
  if not covered:
    return _verdict('not_covered', reason='非廊坊园区，DCIM 告警未覆盖')
  if ts is None:
    return _verdict('no_time', reason='该次发生缺少发生时间，未查询 DCIM 告警')

  cands = _query_alarms(repo_get(), ts)
  cands = _narrow_by_room(cands, _room_codes(meta.get('dc'), name))
  if not cands:
    return _verdict('no_alarm', reason='发生时间前后 2 分钟内没有告警')
  if signature:                           # 复用代表单命中的点位：确定性选中，不调模型
    hit = [c for c in cands if _signature(c) == signature]
    if hit:
      nearest = min(hit, key=lambda a: abs(int(a.get('alarm_time') or 0) - ts))
      return _verdict('matched', 'point', '', alarm=nearest)
  if len(cands) == 1:
    return _verdict('matched', 'time', '', alarm=cands[0])

  cands = _rank_candidates(
    cands, f'{name} {_detail_text(details, meta.get("ticketNo"))}', ts)
  out['llm_calls'] += 1                   # 每次歧义发生各自一次仲裁
  try:
    idx, reason = await picker({
      'name': name, 'describe': _detail_text(details, meta.get('ticketNo')),
      'occurrence_ts': ts, 'park': (rec or {}).get('companyName') or park or '',
      'dc': meta.get('dc') or '', 'level': (rec or {}).get('level') or '',
      'specialty': (rec or {}).get('specialty') or '',
    }, cands)
  except Exception as e:                  # noqa: BLE001 —— 仲裁异常不落表，下次重试
    return _verdict('unknown',
                    reason=f'仲裁调用失败，下次重试：{str(e)[:60]}')
  if not idx:
    return _verdict('ambiguous',
                    reason='多条候选告警，模型未能判定（' + (reason or '证据不足') + '）')
  return _verdict('matched', 'llm', reason, alarm=cands[idx - 1])


async def _link_plan(plan: dict, *, scope: str, park: str | None,
                     company_parks: dict | None, repo_get, picker, details: dict,
                     decisions: dict, force_refresh: bool, identities: dict,
                     out: dict, source: str) -> dict:
  """单个类目的全部发生：先查判定表，未命中才计算（逐次发生）。

  代表单（第一条）照旧走完整流程；其余发生优先复用**点位签名**（判定表里存的或
  本次命中的都算，同一测点再次告警是确定性信号），没有签名时各按自己的时间窗检索。
  命中告警的**身份**收集到 `identities`，由调用方一次性补展示字段。
  """
  name, item = plan['name'], plan['item']
  occ_views: list[dict] = []
  computed: list[dict] = []
  keys: list[tuple] = []
  signature = None
  for occ in plan['occs']:
    ts, rec, meta = occ['ts'], occ['rec'], occ['meta']
    did = alarm_decision.decision_id(meta.get('ticketNo'), ts, scope)
    doc = decisions.get(did) if (did and not force_refresh) else None
    if alarm_decision.is_reusable(doc):
      view = alarm_decision.decision_view(doc)
      out['cache_hits'] += 1
    else:
      verdict = await _match_occurrence(
        rec, meta, ts, name, details, park, company_parks,
        repo_get, picker, out, signature)
      view = dict(verdict)
      if did:
        built = alarm_decision.build_decision(
          ticket_no=meta.get('ticketNo'), occurrence_ts=ts, scope=scope,
          status=view['status'], name=name, dc=meta.get('dc') or '',
          match=view['match'],
          alarm=view['alarm'], point_sig=view['point_sig'],
          alarm_ts=view['alarm_ts'], alarm_snapshot=view['alarm_snapshot'],
          reason=view['reason'], source=source)
        if built:
          computed.append(built)
    if signature is None and view.get('point_sig'):
      signature = tuple(view['point_sig'])
    alarm = view.get('alarm')
    if alarm:
      key = (alarm['cluster'], alarm['event_log_id'])
      if key not in keys:
        keys.append(key)
      info = identities.setdefault(key, {})
      if view.get('alarm_ts') and not info.get('alarm_ts'):
        info['alarm_ts'] = view['alarm_ts']
      if view.get('alarm_snapshot') and not info.get('snapshot'):
        info['snapshot'] = view['alarm_snapshot']
    occ_views.append({**meta, 'status': view.get('status') or '',
                      'match': view.get('match') or '',
                      'reason': view.get('reason') or ''})

  return {'name': name, 'count': item.get('count'),
          'total': len(occ_views),
          'matched_n': sum(1 for v in occ_views if v.get('match')),
          'occurrence': occ_views[0] if occ_views else {},
          'occurrences': occ_views,
          'alarms': [],
          '_keys': keys,
          '_computed': computed}


class _LazyRepo:
  """惰性打开 DCIM 连接：判定全部复用、且没有要展示的告警时不碰 PG。"""

  def __init__(self, factory=None):
    self._factory = factory or _default_repo
    self.repo = None

  def get(self):
    if self.repo is None:
      self.repo = self._factory()
    return self.repo

  def close(self) -> None:
    if self.repo is not None:
      try:
        self.repo.close()
      except Exception:                   # noqa: BLE001 —— 关闭失败不影响结果
        logger.debug('[AlarmLink] 关闭 DCIM 连接失败', exc_info=True)


def _build_plans(rows: list[dict], cur_recs: list[dict]) -> list[dict]:
  """重复类目 → 逐次发生的执行计划（代表单在前）。"""
  plans = []
  for item in rows:
    name = str(item.get('name') or '')
    rec = _pick_record(item, cur_recs)
    occurrences = _occurrences(item, name, cur_recs, rec)
    rep_ts = _to_seconds((rec or {}).get('occurrenceTime'))
    occurrence = {
      'ticketNo': str(item.get('ticketNo') or (rec or {}).get('ticketNo') or ''),
      'occurred_at': _fmt_time(rep_ts) if rep_ts else '',
      'dc': str((rec or {}).get('dc') or ''),
    }
    occs = []
    for i, occ_rec in enumerate(occurrences):
      ts = _to_seconds(occ_rec.get('occurrenceTime'))
      meta = occurrence if i == 0 else {
        'ticketNo': str(occ_rec.get('ticketNo') or ''),
        'occurred_at': _fmt_time(ts) if ts else '',
        'dc': str(occ_rec.get('dc') or ''),
      }
      occs.append({'rec': occ_rec, 'ts': ts, 'meta': meta})
    plans.append({'item': item, 'name': name, 'rec': rec,
                  'occurrence': occurrence, 'occs': occs})
  return plans


def enrich_identities(identities: dict, *, repo=None, repo_factory=None) -> list[dict]:
  """告警身份集合 → 展示视图（值/四档阈值实时补；行被软删时回退判定表快照）。

  `identities` 形如 `{(cluster, event_log_id): {'alarm_ts':…, 'snapshot':…}}`。
  传入已打开的 `repo` 则复用；否则按 `repo_factory` 自建并关闭。
  """
  if not identities:
    return []
  own = repo is None
  if own:
    repo = (repo_factory or _default_repo)()
  try:
    rows = repo.find_alarms_by_ids(list(identities))
    by_key = {(str(r.get('cluster') or ''), str(r.get('event_log_id') or '')): dict(r)
              for r in (rows or [])}
    alarms = []
    for key, info in identities.items():
      row = by_key.get(key) or _snapshot_row(key, (info or {}).get('snapshot'))
      trig = (info or {}).get('alarm_ts')
      if trig:
        row['alarm_time'] = int(trig)     # 展示统一用触发态时间
      alarms.append(row)
    return _enrich_all(repo, alarms)
  finally:
    if own:
      try:
        repo.close()
      except Exception:                   # noqa: BLE001
        logger.debug('[AlarmLink] 关闭 DCIM 连接失败', exc_info=True)


async def link_occurrence(
  *, ticket_no, occurrence_ts, name: str = '', rec: dict | None = None,
  meta: dict | None = None, details: dict | None = None, scope=None,
  company_parks: dict | None = None, repo_factory=None, picker=None,
  store=None, signature: tuple | None = None, force_refresh: bool = False,
  source: str = 'chat',
) -> dict:
  """单次发生：先查判定表，未命中才计算（对话工具复用）。

  返回判定视图（`status` / `match` / `reason` / `alarm` / `alarm_ts` /
  `alarm_snapshot` / `point_sig`）+ `reused` + `decision`（未落表的判定文档，
  调用方批量 `store.save`）。展示字段用 `enrich_identities` 补。
  """
  rec = rec or {}
  ts = _to_seconds(occurrence_ts)
  ticket_no = str(ticket_no or (meta or {}).get('ticketNo') or '').strip()
  meta = meta or {'ticketNo': ticket_no,
                  'occurred_at': _fmt_time(ts) if ts else '',
                  'dc': str(rec.get('dc') or '')}
  did = alarm_decision.decision_id(ticket_no, ts, scope)
  if store is not None and did and not force_refresh:
    try:
      doc = (await store.load([did])).get(did)
    except Exception as e:                # noqa: BLE001 —— 读表失败按未命中
      logger.warning('[AlarmLink] 判定表读取失败：%s', str(e)[:200])
      doc = None
    if alarm_decision.is_reusable(doc):
      return {**alarm_decision.decision_view(doc), 'reused': True,
              'decision': None}

  park = None if alarm_decision.normalize_scope(scope) == 'nationwide' else str(scope)
  if company_parks is None and park is None:
    company_parks = await _company_parks()
  lazy = _LazyRepo(repo_factory)
  out = {'llm_calls': 0}
  try:
    verdict = await _match_occurrence(
      rec, meta, ts, name, details or {}, park, company_parks,
      lazy.get, picker or _default_picker, out, signature)
  finally:
    lazy.close()
  decision = None
  if did:
    decision = alarm_decision.build_decision(
      ticket_no=ticket_no, occurrence_ts=ts, scope=scope, status=verdict['status'],
      name=name, dc=meta.get('dc') or '', match=verdict['match'],
      alarm=verdict['alarm'],
      point_sig=verdict['point_sig'], alarm_ts=verdict['alarm_ts'],
      alarm_snapshot=verdict['alarm_snapshot'], reason=verdict['reason'],
      source=source)
  return {**verdict, 'reused': False, 'decision': decision,
          'llm_calls': out['llm_calls']}


async def link_repeat_alarms(stats: dict, cur_recs: list[dict], details: dict,
                             window, *, park: str | None = None,
                             repo_factory=None, picker=None,
                             company_parks: dict | None = None,
                             store=None, force_refresh: bool = False) -> dict:
  """重复类目 → 关联 DCIM 告警。**不抛异常**；`available=False` 表示数据源不可用。

  **遍历全部重复类目**（不截断），**每次发生先查判定表**：可复用就直接出结论；
  未命中才查 ±`TIME_WINDOW_SECONDS` 候选（代表单先搜），单候选直接接受，
  多候选交 `picker` 仲裁（每次歧义发生各自一次调用），判完批量写回判定表。

  `store` 为 `alarm_decision.AlarmDecisionStore`（`None` 关闭判定表，测试默认关闭）；
  `force_refresh=True` 绕过读取、重算后按同一 `_id` 覆盖写回。
  `company_parks` 注入「法人主体 → 园区」映射（单测用）；为 None 时按需查公司档案。
  """
  out = _base()
  if not settings.DCIM_ENABLED or not settings.DCIM_PG_DSN:
    out['reason'] = 'DCIM 数据接入未启用'
    return out
  win = window if isinstance(window, (tuple, list)) else ()
  end = _as_date(win[1] if len(win) > 1 else None)
  if end is not None and end < ALARM_COVERAGE_START:
    out['reason'] = f'DCIM 告警自 {ALARM_COVERAGE_START} 起才有数据，本期不在覆盖范围'
    return out

  rows = _repeat_rows(stats)
  if not rows:
    out['available'] = True
    return out

  picker = picker or _default_picker
  scope = alarm_decision.normalize_scope(park)
  plans = _build_plans(rows, cur_recs)
  decisions: dict = {}
  if store is not None and not force_refresh:
    ids = [alarm_decision.decision_id(occ['meta'].get('ticketNo'), occ['ts'], scope)
           for plan in plans for occ in plan['occs']]
    try:
      decisions = await store.load(ids)
    except Exception as e:                # noqa: BLE001 —— 读表失败按未命中
      logger.warning('[AlarmLink] 判定表读取失败：%s', str(e)[:200])
      decisions = {}

  def _reusable(occ: dict) -> bool:
    did = alarm_decision.decision_id(occ['meta'].get('ticketNo'), occ['ts'], scope)
    return alarm_decision.is_reusable(decisions.get(did) if did else None)

  missed = any(not _reusable(occ) for plan in plans for occ in plan['occs'])
  if company_parks is None and missed:
    company_parks = await _company_parks() if _needs_company_map(
      rows, cur_recs, park) else {}

  lazy = _LazyRepo(repo_factory)
  identity_info: dict = {}
  computed_all: list[dict] = []
  try:
    built = []
    for plan in plans:
      if not plan['occs']:
        built.append(_unmatched(plan['name'], plan['item'].get('count'),
                                plan['occurrence'],
                                '未找到该类事件记录，未查询 DCIM 告警', total=0))
        continue
      cluster = await _link_plan(
        plan, scope=scope, park=park, company_parks=company_parks,
        repo_get=lazy.get, picker=picker, details=details, decisions=decisions,
        force_refresh=force_refresh, identities=identity_info, out=out,
        source='report')
      computed_all.extend(cluster.pop('_computed', None) or [])
      built.append(cluster)

    view_by_key: dict = {}
    if identity_info:
      for view in enrich_identities(identity_info, repo=lazy.get()):
        view_by_key[(view['cluster'], view['event_log_id'])] = view
    for cluster in built:
      keys = cluster.pop('_keys', None) or []
      cluster['alarms'] = _sort_alarms(
        [view_by_key[k] for k in keys if k in view_by_key])

    if store is not None and computed_all:
      try:
        await store.save(computed_all)
      except Exception as e:              # noqa: BLE001 —— 写表失败不影响报告
        logger.warning('[AlarmLink] 判定表写入失败：%s', str(e)[:200])

    out['clusters'] = built
    out['linked_n'] = sum(1 for c in built if c['alarms'])
    out['available'] = True
    return out
  except Exception as e:                  # noqa: BLE001 —— 报告不能因关联失败而失败
    logger.warning('[AlarmLink] 重复类目关联告警失败：%s', str(e)[:300])
    failed = _base()
    failed['reason'] = f'读取 DCIM 告警失败：{str(e)[:120]}'
    failed['clusters'] = out['clusters']
    failed['llm_calls'] = out['llm_calls']
    failed['cache_hits'] = out['cache_hits']
    failed['linked_n'] = out['linked_n']
    return failed
  finally:
    lazy.close()
