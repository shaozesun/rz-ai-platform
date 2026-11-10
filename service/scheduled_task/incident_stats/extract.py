"""报文字段抽取兜底：确定性规则取不到时，让模型从原文抽「发生时间 / 告警值」。

为什么要有它：现场报文格式并不统一（华为 eWatch 推送有单行/多行两种、人工速报的
标签五花八门），确定性正则总会漏。**漏了不能瞎填** —— 以前用消息 `create_time` 兜底，
表里「发生时间」与「速报时间」就一模一样，还把 16:07 的事故显示成 17:14。

三条约束：
1. 只在**确定性解析失败**时调用（已有值绝不覆盖）；
2. 模型给的时刻必须再过 `feishu.value_to_epoch` 且落在合理区间，否则丢弃 ——
   宁可留空，也不凭空写一个时刻（用户口径「识别不出来的不要乱填写」）；
3. 同样的报文只算一次：结果按 `sha1` 缓存在 Redis。`sync` 每轮带 60s 重叠窗口、
   回补也会重复拉到同一条消息，不缓存等于每轮重复烧钱。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from config.redis_conn import redis_manager
from config.settings import settings
from service.scheduled_task.incident_stats import feishu

logger = logging.getLogger(__name__)

_TZ = ZoneInfo('Asia/Shanghai')
_MAX_TEXT = 1200             # 原文截断：速报/通报通常 300 字内，卡片长文的字段都在前面
_MAX_TOKENS = 120
_CACHE_PREFIX = 'incident:extract:'
_CACHE_TTL = 30 * 86400
_MAX_AHEAD_SECONDS = 300     # 发生时间不能比消息晚 5 分钟以上
_MAX_BEHIND_SECONDS = 7 * 86400   # 补录最多追 7 天
_MAX_VALUE = 1e9

_SYSTEM = (
  '你是报文信息抽取器。只从用户给的原文里抽取字段，禁止推测、换算、补全。\n'
  '输出 JSON：{"event_time": "<YYYY-MM-DD HH:MM:SS>" 或 null,'
  ' "report_value": <数字> 或 null}。\n'
  'event_time 是告警/事件**发生**的时刻；原文里的「恢复时间」「预计完成时间」都不是它。\n'
  '抽不到就返回 null，不要编造。'
)

_FENCE_RE = re.compile(r'^```[a-zA-Z]*\s*|\s*```$')
_JSON_RE = re.compile(r'\{[\s\S]*\}')


def _loads(raw: str) -> dict:
  text = _FENCE_RE.sub('', (raw or '').strip())
  try:
    data = json.loads(text)
  except (TypeError, ValueError):
    match = _JSON_RE.search(text)
    if not match:
      return {}
    try:
      data = json.loads(match.group())
    except (TypeError, ValueError):
      return {}
  return data if isinstance(data, dict) else {}


def needs_llm(row: dict) -> bool:
  """确定性解析缺字段才值得调模型；`other` 不进统计表，永远不调。"""
  if row.get('kind') not in ('sos', 'first'):
    return False
  if row.get('event_time') is None:
    return True
  return row.get('kind') == 'sos' and row.get('report_value') is None


def _valid_ts(value, ref_ts: int | None) -> int | None:
  """模型给的时刻 → epoch；确定性解析过不了、或超出合理区间，一律丢弃。"""
  if value is None or isinstance(value, bool):
    return None
  ts = feishu.value_to_epoch(str(value), ref_ts)
  if not ts:
    logger.warning('[Incident] 抽取的时间解析不出来，丢弃：%s', str(value)[:60])
    return None
  if ref_ts:
    ref = int(ref_ts)
    if ts > ref + _MAX_AHEAD_SECONDS or ref - ts > _MAX_BEHIND_SECONDS:
      logger.warning('[Incident] 抽取的时间超出合理区间，丢弃：%s（消息 %s）',
                     str(value)[:60], ref)
      return None
  return ts


def _valid_value(value) -> float | None:
  if value is None or isinstance(value, bool):
    return None
  try:
    out = float(value)
  except (TypeError, ValueError):
    return None
  if out != out or abs(out) > _MAX_VALUE:   # NaN / 离谱量级
    return None
  return out


def normalize(row: dict, data: dict) -> dict:
  """模型 JSON → `{'t': epoch|0, 'v': float|None}`（已校验、可缓存）。"""
  return {
    't': _valid_ts(data.get('event_time'), row.get('create_time')) or 0,
    'v': _valid_value(data.get('report_value')),
  }


def apply(row: dict, norm: dict) -> bool:
  """把校验过的值写回行：**只填空字段**，返回是否有改动。"""
  changed = False
  if row.get('event_time') is None and norm.get('t'):
    row['event_time'] = int(norm['t'])
    changed = True
  if row.get('report_value') is None and norm.get('v') is not None:
    row['report_value'] = float(norm['v'])
    changed = True
  return changed


def _cache_key(row: dict) -> str:
  """消息 id + 原文的摘要：同一条消息重拉命中同一条缓存，不同消息不会撞。"""
  seed = f"{row.get('kind')}|{row.get('message_id')}|{row.get('text')}"
  return _CACHE_PREFIX + hashlib.sha1(seed.encode('utf-8')).hexdigest()


def _cache_read(row: dict) -> dict | None:
  try:
    raw = redis_manager.client.get(redis_manager.key(_cache_key(row)))
  except Exception:
    return None          # Redis 不可用 → fail-open，照常调模型
  if not raw:
    return None
  try:
    data = json.loads(raw)
  except (TypeError, ValueError):
    return None
  return data if isinstance(data, dict) else None


def _cache_write(row: dict, norm: dict) -> None:
  """只缓存**正常返回**的结果（含「模型也抽不到」）；传输失败不写，下一轮还要再试。"""
  try:
    redis_manager.client.set(redis_manager.key(_cache_key(row)),
                             json.dumps(norm, ensure_ascii=False), ex=_CACHE_TTL)
  except Exception as e:
    logger.debug('[Incident] 抽取结果缓存写入失败：%s', e)


def _prompt(row: dict) -> str:
  text = str(row.get('text') or '')[:_MAX_TEXT]
  create_s = row.get('create_time')
  stamp = ''
  if create_s:
    stamp = datetime.fromtimestamp(int(create_s), _TZ).strftime('%Y-%m-%d %H:%M:%S')
  return f'消息发送时间：{stamp or "未知"}\n原文：\n{text}'


async def _one(row: dict) -> bool:
  """单行：缓存 → 模型 → 校验 → 写回。返回是否真的补上了字段。"""
  norm = _cache_read(row)
  if norm is None:
    from core.model_gateway import model_gateway

    try:
      raw = await model_gateway.chat(
        messages=[{'role': 'system', 'content': _SYSTEM},
                  {'role': 'user', 'content': _prompt(row)}],
        temperature=0, max_tokens=_MAX_TOKENS, json_mode=True,
        timeout=int(settings.INCIDENT_EXTRACT_LLM_TIMEOUT),
        caller='incident_extract',
      )
    except Exception as e:
      logger.warning('[Incident] 报文字段抽取调用失败 msg=%s: %s',
                     row.get('message_id'), str(e)[:200])
      return False
    data = _loads(raw)
    if not data:
      # 模型输出不是可用 JSON：不缓存，留给下一轮（可能只是这一次抽风）
      logger.warning('[Incident] 报文抽取返回非 JSON msg=%s: %s',
                     row.get('message_id'), str(raw)[:120])
      return False
    norm = normalize(row, data)
    _cache_write(row, norm)

  changed = apply(row, norm)
  if changed:
    logger.info('[Incident] 模型补全报文字段 msg=%s event_time=%s value=%s',
                row.get('message_id'), row.get('event_time'), row.get('report_value'))
  return changed


async def enrich_rows(rows: list[dict], max_calls: int | None = None) -> int:
  """批量补字段（串行、有上限）。返回被补全的行数；任何异常都不影响入库。"""
  if not settings.INCIDENT_EXTRACT_LLM_ENABLED or not rows:
    return 0
  budget = int(max_calls if max_calls is not None
               else settings.INCIDENT_EXTRACT_LLM_MAX_PER_ROUND)
  filled = 0
  for row in rows:
    if budget <= 0:
      break
    if not needs_llm(row):
      continue
    budget -= 1
    try:
      if await _one(row):
        filled += 1
    except Exception:
      logger.exception('[Incident] 报文字段抽取异常 msg=%s', row.get('message_id'))
  return filled
