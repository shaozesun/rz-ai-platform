"""并列候选的模型仲裁（**一次独立模型调用，不经 Agent**）。

只在结构化手段**已经分不出**时介入：同一秒有多条告警、报文里的告警值也收不窄
（`sync._pick_candidate` 返回 None）。此时把通报原文与候选告警一起给模型，让它按
地点 / 设备 / 测点 / 告警项判断是哪一条。

**刻意不走 Agent**：这里只需要 1~2KB 提示词与一次 `model_gateway.chat()`；走 Agent
会带上几万 token 的系统提示词和工具清单（与本需求无关，且每条通报要重复付一次）。

判不出 → `pick` 返回 None，调用方留空（沿用「不配优于配错」，统计行照出）。
"""

from __future__ import annotations

import json
import logging
import re
import time

logger = logging.getLogger(__name__)

_MAX_TEXT = 800              # 通报原文截断（速报/通报通常 300 字内）
_MAX_REASON = 60
_MAX_TOKENS = 220
_TIMEOUT = 15

_SYSTEM = (
  '你是数据中心告警匹配助手。用户会给出一条飞书通报和若干条候选告警，'
  '这些候选的触发时间都与通报发生时间完全一致，请判断这条通报对应哪一条告警。\n'
  '判断依据：地点（楼宇 / 机房 / 空间名）、设备名称、测点或告警项、告警值。\n'
  '只能从候选中选，不得臆造；证据不足时 pick 必须为 null。\n'
  '只输出 JSON：{"pick": <候选序号或 null>, "reason": "<不超过60字>"}'
)

_PICK_RE = re.compile(r'"pick"\s*:\s*(null|-?\d+)', re.I)
_FENCE_RE = re.compile(r'^```[a-zA-Z]*\s*|\s*```$')

# 卡片原地追加的进展块起点：`进展同步1`（带数字）或 `【进展描述】`；前面的
# `－－－－` 分隔线一并吃掉。**不带数字的 `【进展同步】` 不是切点** —— 它是卡片的
# 原地字段，后面紧跟【事件来源】/【涉及客户】/【影响范围】/【冗余配置】/【系统状态】，
# 其中「影响范围」是仲裁的重要依据。
_PROGRESS_CUT_RE = re.compile(r'(?:[－-]{2,}\s*)?进展同步\s*\d+|【\s*进展描述')


def _strip_progress(text: str) -> str:
  """去掉通报末尾追加的进展块，只留首版通报正文（仲裁输入用；展示仍用全文）。"""
  match = _PROGRESS_CUT_RE.search(text or '')
  return (text or '')[:match.start()].rstrip(' －-\n\t') if match else (text or '')


def _fmt_time(ts) -> str:
  try:
    return time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(int(ts)))
  except (TypeError, ValueError, OSError):
    return ''


def build_prompt(report: dict, cands: list[dict]) -> tuple[str, str]:
  """→ `(system, user)`；候选按传入顺序编号 1..N（调用方按 `event_log_id` 排序传入，
  只为编号稳定）。

  不把机房的**抽取结果**给模型：正则经常抽不到、或抽出 `A1-1` 这类垃圾码，
  同一个错误值会带偏仲裁。空间名（告警侧）照常给，模型自己对照通报原文判断。
  """
  text = _strip_progress(str(report.get('text') or ''))
  head = {
    '通报发生时间': _fmt_time(report.get('event_time')),
    '通报告警值': report.get('report_value'),
    '通报原文': text[:_MAX_TEXT],
  }
  items = []
  for i, cand in enumerate(cands, 1):
    value = cand.get('value')
    items.append({
      '序号': i,
      '触发时间': _fmt_time(cand.get('trigger_time')),
      '触发告警值': value,
      '空间名': cand.get('space') or '',
      '设备': cand.get('device_name') or '',
      '测点': cand.get('point_name') or '',
      '告警内容': cand.get('content') or '',
      '告警级别': cand.get('level') or '',
    })
  user = json.dumps({'通报': head, '候选告警': items}, ensure_ascii=False, indent=1)
  return _SYSTEM, user


def _loads(raw: str) -> dict:
  text = _FENCE_RE.sub('', (raw or '').strip())
  try:
    out = json.loads(text)
  except (TypeError, ValueError):
    return {}
  return out if isinstance(out, dict) else {}


def _valid_pick(value, n: int) -> int | None:
  if isinstance(value, bool):
    return None
  try:
    idx = int(value)
  except (TypeError, ValueError):
    return None
  return idx if 1 <= idx <= n else None


def parse_pick(raw: str, n: int) -> int | None:
  """模型回复 → 候选序号（1 起）；`null` / 越界 / 垃圾一律 None。"""
  data = _loads(raw)
  if data:
    return _valid_pick(data.get('pick'), n)
  match = _PICK_RE.search(raw or '')
  if not match:
    return None
  if match.group(1).lower() == 'null':
    return None
  return _valid_pick(match.group(1), n)


def all_identical(cands: list[dict]) -> bool:
  """并列候选的地点/设备/测点/告警内容**完全相同** → 上游按点位去重可能坏了。"""
  if len(cands) < 2:
    return False
  keys = {
    (str(c.get('space') or ''), str(c.get('device_name') or ''),
     str(c.get('point_name') or ''), str(c.get('content') or ''))
    for c in cands
  }
  return len(keys) == 1


async def pick(report: dict, cands: list[dict]) -> tuple[int | None, str]:
  """一次独立模型调用 → `(候选序号 1..N | None, reason)`；失败 fail-open 返回 None。"""
  from core.model_gateway import model_gateway

  system, user = build_prompt(report, cands)
  try:
    raw = await model_gateway.chat(
      messages=[{'role': 'system', 'content': system},
                {'role': 'user', 'content': user}],
      temperature=0, max_tokens=_MAX_TOKENS, json_mode=True, timeout=_TIMEOUT,
      caller='incident_match',
    )
  except Exception as e:
    logger.warning('[Incident] LLM 仲裁调用失败 report=%s: %s',
                   report.get('id'), str(e)[:200])
    return None, ''
  reason = str(_loads(raw).get('reason') or '')[:_MAX_REASON]
  return parse_pick(raw, len(cands)), reason
