"""飞书通报增量同步 + 未匹配告警重试。

游标：每个群取已入库的最大 `create_time` 再往前重叠 60s（UNIQUE 兜住重复），
不另建状态表；拉取失败不推进，下一轮自然重试。

后台循环只在 `INCIDENT_STATS_ENABLED=true` 时启动，多 worker 用 Redis 选主；
Redis 抖动时 fail-open（同步本身幂等，重复拉不会产生重复行）。
"""

import asyncio
import logging
import time

from config.redis_conn import redis_manager
from config.settings import settings
from service.scheduled_task.incident_stats import arbitrate, extract, feishu, schema, source

logger = logging.getLogger(__name__)

_OVERLAP_SECONDS = 60
_SYNC_INTERVAL_DEFAULT = 300


# 匹配口径（2026-11-04 用户拍板）：**时间唯一必需**，差 1 秒也不算；
# 告警值只在报文提供时、用于同秒多候选的副筛选；仍分不出交模型仲裁。
# 机房编码不参与匹配 —— 正则抽不到 `B1M2-4F-402` 这类形态，且比对的归一化会
# 删连字符（`A1-1` → `a11` 命中 `A11数据中心`），假命中会把候选收窄到错误的一条。
_TIME_HIT_SECONDS = 0
_VALUE_TOL_ABS = 0.01
_VALUE_TOL_REL = 0.005


def _to_int(value) -> int | None:
  try:
    return int(value)
  except (TypeError, ValueError):
    return None


def _to_float(value) -> float | None:
  try:
    return float(value)
  except (TypeError, ValueError):
    return None


def _signals(report: dict, alarm: dict) -> tuple[int, str]:
  """结构化信号：时间（唯一必需）/ 告警值（仅同秒副筛选），返回 `(命中数, 信号名)`。

  文本不参与匹配：通报的「事件概述」是人工写的，和告警原文措辞常常不同；同秒
  多条由告警值收窄，仍分不出交模型仲裁。
  """
  hits: list[str] = []
  event_ts = _to_int(report.get('event_time'))
  alarm_ts = _to_int(alarm.get('trigger_time') or alarm.get('alarm_time'))
  if event_ts and alarm_ts and abs(alarm_ts - event_ts) <= _TIME_HIT_SECONDS:
    hits.append('time')

  report_value = _to_float(report.get('report_value'))
  alarm_value = _to_float(alarm.get('trigger_value'))
  if alarm_value is None:
    alarm_value = _to_float(alarm.get('alarm_value'))
  if report_value is not None and alarm_value is not None:
    scale = max(abs(report_value), abs(alarm_value))
    if abs(report_value - alarm_value) <= max(_VALUE_TOL_ABS, _VALUE_TOL_REL * scale):
      hits.append('value')

  return len(hits), '+'.join(hits)


def _narrow_by_value(cands: list[dict], report_value: float | None) -> list[dict]:
  """同秒多候选：报文里有【告警值】才拿它收窄，没有就原样返回（只按时间）。

  收窄后为空（多半是单位/格式差异，如 mA vs A）**不反向否决**，保留原候选组 ——
  否则一条写错单位的值会让整条通报配不上。
  """
  if report_value is None:
    return cands
  kept = [c for c in cands if 'value' in str(c.get('detail') or '').split('+')]
  return kept or cands


def _pick_candidate(cands: list[dict],
                    report_value: float | None = None) -> tuple[str, int, str] | None:
  """决策树：时间已保证同秒；单候选直接配；多条只用告警值收窄，收不窄交模型。

  1. 0 条 → 不配
  2. 1 条 → 配（`detail` 照实，如 `time` / `time+value`）
  3. 多条 → 报文有告警值就用它收窄；收窄到唯一一条 → 配
  4. 仍多条 → None（并列组交给 `match_pending` 走模型仲裁，见 `arbitrate.py`）
  """
  if not cands:
    return None
  if len(cands) == 1:
    only = cands[0]
    return str(only['event_log_id']), int(only['hits']), str(only['detail'] or 'single')
  narrowed = _narrow_by_value(cands, report_value)
  if len(narrowed) == 1:
    only = narrowed[0]
    return str(only['event_log_id']), int(only['hits']), str(only['detail'])
  return None


def _tied_group(cands: list[dict]) -> list[dict]:
  """交给模型的并列组：按 `event_log_id` 升序，只为编号稳定，**不参与取舍**。"""
  return sorted(cands, key=lambda c: str(c.get('event_log_id') or ''))


def _sibling_pick(cands: list[dict], siblings: set[str] | None
                  ) -> tuple[str, int, str] | None:
  """兄弟证据：同群同秒的另一条通报已配上唯一 guid，且该 guid 就在并列组里 → 直接采用。

  速报与首报是同一次事件的两条消息，速报正文带【告警值】通常能收窄，首报（卡片）
  没有；让后者继承前者的结论，零模型调用。兄弟出现多个 guid（同秒两起事件）不继承。
  """
  if not siblings or len(siblings) != 1:
    return None
  guid = next(iter(siblings))
  kept = [c for c in cands if str(c.get('event_log_id')) == guid]
  if len(kept) != 1:
    return None
  only = kept[0]
  return str(guid), int(only['hits']), f'{only["detail"]}+sibling'


def match_one(report: dict, siblings: set[str] | None = None
              ) -> tuple[tuple[str, int, str] | None, list[dict]]:
  """一条通报 → `(命中结果, 并列候选组)`。

  时间**精确到秒**是唯一必需信号：报文没解析出【发生时间】就不配（不拿消息
  `create_time` 硬凑）。命中结果 `(event_log_id, 信号数, 命中信号)`；结构化分不出时
  先看兄弟证据（`_sibling_pick`），仍分不出才把并列组交给模型仲裁 —— 命中时该组
  无意义（返回空）。
  """
  cluster = (report.get('cluster') or '').strip()
  event_ts = _to_int(report.get('event_time'))
  if not cluster or not event_ts:
    return None, []
  candidates = source.alarm_candidates(cluster, event_ts, event_ts)
  if not candidates:
    return None, []

  guids = [str(c.get('event_log_id')) for c in candidates if c.get('event_log_id')]
  first = source.first_push_info(cluster, guids)
  names = source.space_names([
    (c.get('cluster') or cluster, c.get('space_guid'))
    for c in candidates if c.get('space_guid')
  ])

  scored: list[dict] = []
  for candidate in candidates:
    event_log_id = str(candidate.get('event_log_id') or '')
    if not event_log_id:
      continue
    merged = dict(candidate)
    info = first.get(event_log_id) or {}
    # 用触发态的时间和值（当前态会被刷新/恢复推送覆盖）
    if info.get('timestamp'):
      merged['trigger_time'] = info['timestamp']
    if info.get('value') is not None:
      merged['trigger_value'] = info['value']
    for key in ('content', 'device_name', 'point_name'):
      if info.get(key):
        merged[key] = info[key]
    # 触发态时间也必须与发生时间完全一致（当前态 alarm_time 是触发时间，这里
    # 再挡一次 fallback/脏数据：history 里取到的 timestamp 不一定是触发态）
    trigger_ts = _to_int(merged.get('trigger_time') or merged.get('alarm_time'))
    if trigger_ts != event_ts:
      continue
    space = names.get((candidate.get('cluster') or cluster, candidate.get('space_guid')), '')
    hits, detail = _signals(report, merged)
    if not info:
      detail = f'{detail}+fallback' if detail else 'fallback'
    # 展示字段留给模型仲裁（`arbitrate.build_prompt`），不进匹配决策
    value = merged.get('trigger_value')
    scored.append({
      'event_log_id': event_log_id, 'hits': hits, 'detail': detail,
      'trigger_time': trigger_ts,
      'value': value if value is not None else merged.get('alarm_value'),
      'space': space,
      'device_name': merged.get('device_name') or '',
      'point_name': merged.get('point_name') or '',
      'content': merged.get('content') or '',
      'level': merged.get('alarm_level') or '',
    })
  report_value = _to_float(report.get('report_value'))
  hit = _pick_candidate(scored, report_value)
  if hit:
    return hit, []
  tied = _tied_group(_narrow_by_value(scored, report_value))
  picked = _sibling_pick(tied, siblings)
  if picked:
    return picked, []
  return None, tied


def _can_arbitrate(report: dict, tied: list[dict]) -> bool:
  """结构化分不出时，是否允许为这条通报调模型（开关/只试一次/字段全同）。"""
  if not settings.INCIDENT_MATCH_LLM_ENABLED:
    return False
  if len(tied) < 2 or report.get('llm_tried_at') is not None:
    return False
  if arbitrate.all_identical(tied):
    # 同一时刻同一测点只可能有一条告警；真出现说明上游按点位去重坏了 → 不猜
    logger.warning('[Incident] 裁决跳过（并列候选字段完全相同）report=%s', report['id'])
    return False
  return True


async def _arbitrate_one(report: dict, tied: list[dict]) -> str | None:
  """调模型仲裁并落库；返回配上的 guid（判不出为 None）。

  **调用过就标 `llm_tried_at`**（一条只试一次）。返回 guid 让同轮后到的兄弟通报
  可以直接继承，不必再调一次模型。
  """
  pick, reason = await arbitrate.pick(report, tied)
  source.mark_llm_tried(int(report['id']))
  if not pick:
    logger.info('[Incident] LLM 仲裁留空 report=%s reason=%s', report['id'], reason or '-')
    return None
  chosen = tied[pick - 1]
  event_log_id = str(chosen['event_log_id'])
  source.update_match(int(report['id']), event_log_id, int(chosen['hits']),
                      f'{chosen["detail"]}+llm')
  logger.info('[Incident] LLM 仲裁 report=%s pick=%d → %s reason=%s',
              report['id'], pick, event_log_id, reason or '-')
  return event_log_id


# 同一进程里的并发入口（实时去抖 / 补偿回补 / 出表前补一次）共用一把锁：
# 结构匹配重复跑无副作用，但模型仲裁会重复烧钱（`llm_tried_at` 写在调用之后，
# 两个并发轮次会同时看到空值）。
_match_lock = asyncio.Lock()


async def match_pending(limit: int = 500) -> int:
  """给未匹配的通报补 event_log_id（Kafka 可能晚于群消息落库）。返回匹配条数。

  结构匹配在 `to_thread` 里跑（psycopg 同步）；分不出并列时先看兄弟证据，再逐条
  `await arbitrate.pick`。**不设每轮仲裁预算**：积压一次跑完，被跳过的行不标
  `llm_tried_at`，下一轮自然重试。结构重试照旧（迟到告警仍能被信号配上）。
  """
  async with _match_lock:
    return await _match_pending(limit)


async def _match_pending(limit: int) -> int:
  rows = source.pending_reports(limit)
  try:
    siblings = source.matched_siblings([
      (r.get('chat_id'), r.get('event_time')) for r in rows
    ])
  except Exception as e:
    # 兄弟证据只是优化，查不到不影响结构化匹配
    logger.warning('[Incident] 读取兄弟匹配失败，本轮不做兄弟继承: %s', e)
    siblings = {}
  matched = 0
  for report in rows:
    key = (report.get('chat_id') or '', _to_int(report.get('event_time')))
    try:
      hit, tied = await asyncio.to_thread(match_one, report, siblings.get(key))
      if not hit and not _can_arbitrate(report, tied):
        continue
      if hit:
        event_log_id, score, detail = hit
        source.update_match(int(report['id']), event_log_id, score, detail)
      else:
        event_log_id = await _arbitrate_one(report, tied)
        if not event_log_id:
          continue
    except Exception as e:
      # 单条失败不中断整轮：一条 SQL/数据错误曾让整批积压永远等不到重试
      logger.warning('[Incident] 匹配单条失败 report=%s: %s',
                     report.get('id'), str(e)[:200])
      continue
    # 本轮的匹配结果也记进兄弟表 —— 同秒的速报先配、首报随后直接继承
    siblings.setdefault(key, set()).add(event_log_id)
    matched += 1
  if rows:
    logger.info('[Incident] 通报匹配：待匹配 %d，本轮成功 %d', len(rows), matched)
  return matched


def _delete_recalled(chat_id: str, message_ids: list[str]) -> int:
  """撤回消息 → 物理删行（当没发过）。逐条删，单条失败只记日志（下一轮还会重试）。"""
  deleted = 0
  for message_id in message_ids:
    try:
      deleted += source.delete_report(chat_id, message_id)
    except Exception as e:
      logger.warning('[Incident] 删除撤回消息失败 chat=%s msg=%s: %s', chat_id, message_id, e)
  return deleted


async def sync_range(start_s: int, end_s: int | None = None, *,
                     backfill: bool = False) -> int:
  """同步 [start_s, end_s) 的通报并补匹配；返回新入库条数。

  `backfill=True` 忽略「已入库最大时间」游标，按给定窗口整段重拉（首次进群回填用；
  否则后台循环先跑过之后，再回填更早的窗口会被游标挡住）。
  """
  schema.ensure_reports_schema(settings.DCIM_PG_DSN)
  end = int(end_s or time.time())
  chats = await feishu.resolve_park_chats()
  if not chats:
    logger.info('[Incident] 未配置园区群（INCIDENT_FEISHU_PARK_CHATS），跳过同步')
    return 0

  inserted = 0
  for park, chat_id in chats.items():
    latest = None if backfill else source.latest_report_time(chat_id)
    since = max(int(start_s), (latest or 0) - _OVERLAP_SECONDS)
    try:
      messages, recalled = await feishu.fetch_messages(chat_id, since, end)
    except Exception as e:
      logger.warning('[Incident] 拉取群消息失败 park=%s chat=%s: %s', park, chat_id, e)
      continue
    for message in messages:
      message['cluster'] = park
    # 确定性解析缺发生时间/告警值的，让模型补一次（有上限；结果同样要过日期校验）
    filled = await extract.enrich_rows(messages)
    if filled:
      logger.info('[Incident] park=%s 模型补全报文字段 %d 条', park, filled)
    try:
      inserted += source.save_reports(messages)
    except Exception as e:
      logger.warning('[Incident] 写入通报失败 park=%s: %s', park, e)
      continue
    deleted = _delete_recalled(chat_id, recalled)
    logger.info('[Incident] park=%s 拉取 %d 条（新增 %d，撤回 %d）',
                park, len(messages), inserted, deleted)

  await match_pending()
  return inserted


_WATERMARK_PREFIX = 'incident:backfill:ts:'
_WATERMARK_TTL = 30 * 86400


def _watermark(chat_id: str) -> int | None:
  """该群上次**成功回补完**的时间点（Redis；不可用/无记录返回 None）。"""
  try:
    raw = redis_manager.client.get(redis_manager.key(_WATERMARK_PREFIX + chat_id))
    return int(raw) if raw else None
  except Exception as e:
    logger.warning('[Incident] 读取回补水印失败 chat=%s: %s', chat_id, e)
    return None


def _set_watermark(chat_id: str, ts: int) -> None:
  try:
    redis_manager.client.set(
      redis_manager.key(_WATERMARK_PREFIX + chat_id), ts, ex=_WATERMARK_TTL,
    )
  except Exception as e:
    logger.warning('[Incident] 写入回补水印失败 chat=%s: %s', chat_id, e)


def backfill_since(watermark: int | None, db_latest: int | None, *,
                   now: int, cap_seconds: int) -> int:
  """缺口回补的起点（纯函数，便于离线断言）。

  取两个锚点的**最小值**：实时写入会把库内最新消息推到「此刻」，取 max 就永远
  跳过了中间的空洞。两个锚点都没有（新群 / Redis 与库都空）时直接追满 cap。
  """
  floor = now - max(0, int(cap_seconds))
  anchors = [int(x) for x in (watermark, db_latest) if x]
  if not anchors:
    return floor
  return max(min(anchors) - _OVERLAP_SECONDS, floor)


async def sync_gaps(cap_seconds: int) -> int:
  """按「上次成功补到的时间」回补断线缺口，返回新入库条数。

  与 `sync_range` 的区别：起点**逐群算**（水位 vs 库内最新取小），不是全局固定窗口
  —— 停机两小时后重启也能把整段缺口补回来（上限 `cap_seconds`）。
  拉取失败/写入失败的群不推进水位，下一轮自然重试。
  """
  schema.ensure_reports_schema(settings.DCIM_PG_DSN)
  chats = await feishu.resolve_park_chats()
  if not chats:
    logger.info('[Incident] 未配置园区群（INCIDENT_FEISHU_PARK_CHATS），跳过回补')
    return 0

  now = int(time.time())
  inserted = 0
  for park, chat_id in chats.items():
    since = backfill_since(
      _watermark(chat_id), source.latest_report_time(chat_id),
      now=now, cap_seconds=cap_seconds,
    )
    try:
      messages, recalled = await feishu.fetch_messages(chat_id, since, now)
    except Exception as e:
      logger.warning('[Incident] 回补拉取失败 park=%s chat=%s: %s', park, chat_id, e)
      continue
    for message in messages:
      message['cluster'] = park
    filled = await extract.enrich_rows(messages)
    if filled:
      logger.info('[Incident] 回补 park=%s 模型补全报文字段 %d 条', park, filled)
    try:
      inserted += source.save_reports(messages)
    except Exception as e:
      logger.warning('[Incident] 回补写入失败 park=%s: %s', park, e)
      continue
    deleted = _delete_recalled(chat_id, recalled)
    _set_watermark(chat_id, now)
    logger.info(
      '[Incident] 回补 park=%s 起点 %s 拉取 %d 条（新增 %d，撤回 %d）',
      park, time.strftime('%m-%d %H:%M', time.localtime(since)), len(messages), inserted,
      deleted,
    )

  await match_pending()
  return inserted


_CARD_RUN_KEY = 'incident:card-refresh:run'
_CARD_LAST_KEY = 'incident:card-refresh:last'
_CARD_LEASE_SECONDS = 900
_CARD_LAST_TTL = 7 * 86400
_CARD_POLL_SECONDS = 2


async def refresh_cards(hours: int | None = None) -> int:
  """回捞窗口内 interactive 卡片的当前正文，变了才回写。返回更新条数。

  卡片通报会在**同一条消息**里原地追加「进展同步N /【进展描述】」，飞书没有消息
  更新事件、增量游标也不会回看旧消息，只能逐条 `message.get` 取当前正文。
  """
  hours = int(hours if hours is not None else settings.INCIDENT_CARD_REFRESH_HOURS)
  since = int(time.time()) - max(1, hours) * 3600
  try:
    reports = source.interactive_reports(since)
  except Exception as e:
    logger.warning('[Incident] 卡片回捞取数失败: %s', e)
    return 0
  updated = 0
  for report in reports:
    message_id = report.get('message_id') or ''
    if not message_id:
      continue
    try:
      fetched = await feishu.get_message(message_id)
    except Exception as e:
      logger.warning('[Incident] 卡片回捞单条失败 msg=%s: %s', message_id, str(e)[:200])
      continue
    if not fetched:
      continue
    text, raw = fetched
    if (text or '').strip() == (report.get('text') or '').strip():
      continue
    # 只在库里没有发生时间时补：已有值一律保留，匹配口径不动
    event_time = None
    if report.get('event_time') is None:
      event_time = feishu.extract_event_time(text, report.get('create_time'))
    try:
      source.update_report_text(report['id'], text, raw, event_time)
    except Exception as e:
      logger.warning('[Incident] 卡片回捞写入失败 msg=%s: %s', message_id, str(e)[:200])
      continue
    updated += 1
  if updated:
    logger.info('[Incident] 卡片进展回捞：窗口 %dh，更新 %d 条', hours, updated)
  return updated


async def refresh_cards_coalesced(hours: int | None = None, max_age_s: int = 600,
                                  wait_s: int = 180) -> int:
  """多 worker / 多园区同时触发时只跑一份；近 `max_age_s` 内跑过直接返回。

  单飞靠 Redis 而不用 `LeaderLock`：出表任务也要能触发，不只是 leader。抢不到就
  轮询等赢家完成，避免「另一个正在刷新」时按旧正文出表。Redis 不可用 fail-open 直接跑。
  """
  client = redis_manager.client
  run_key = redis_manager.key(_CARD_RUN_KEY)
  last_key = redis_manager.key(_CARD_LAST_KEY)
  started = int(time.time())
  try:
    last_raw = client.get(last_key)
    if last_raw and started - int(last_raw) <= max(0, int(max_age_s)):
      return 0
    acquired = bool(client.set(run_key, started, nx=True, ex=_CARD_LEASE_SECONDS))
  except Exception as e:
    logger.warning('[Incident] 卡片回捞单飞锁不可用，直接执行: %s', e)
    acquired = True
  if acquired:
    try:
      return await refresh_cards(hours)
    finally:
      try:
        client.set(last_key, int(time.time()), ex=_CARD_LAST_TTL)
        client.delete(run_key)
      except Exception as e:
        logger.warning('[Incident] 卡片回捞单飞锁释放失败: %s', e)
  deadline = time.time() + max(0, int(wait_s))
  while time.time() < deadline:
    await asyncio.sleep(_CARD_POLL_SECONDS)
    try:
      last_raw = client.get(last_key)
      if last_raw and int(last_raw) >= started:
        return 0
      if not client.exists(run_key):
        return 0
    except Exception:
      return 0
  logger.warning('[Incident] 等待卡片回捞完成超时（%ss），按现有正文出表', wait_s)
  return 0


_card_background: asyncio.Task | None = None


async def _card_loop() -> None:
  interval = max(60, int(settings.INCIDENT_CARD_REFRESH_MINUTES or 60) * 60)
  while True:
    try:
      await refresh_cards_coalesced(max_age_s=interval // 2, wait_s=min(150, interval))
    except Exception:
      logger.exception('[Incident] 卡片进展回捞失败（下一轮重试）')
    await asyncio.sleep(interval)


async def start_card_refresh() -> None:
  """启动卡片进展回捞（幂等）：事件统计同步或实时入库任一路启用即可。

  `INCIDENT_CARD_REFRESH_MINUTES <= 0` 时不启动：现场进展是「每条新消息」，WS 已实时
  入库，定点回捞没有收益还会覆盖干净正文（仅原地编辑卡片的园区才需要开启）。
  """
  global _card_background
  if int(settings.INCIDENT_CARD_REFRESH_MINUTES or 0) <= 0:
    return
  if _card_background is not None and not _card_background.done():
    return
  interval = max(60, int(settings.INCIDENT_CARD_REFRESH_MINUTES or 60) * 60)
  _card_background = asyncio.create_task(_card_loop())
  logger.info('[Incident] 卡片进展回捞任务已启动（间隔 %ss）', interval)


async def stop_card_refresh() -> None:
  global _card_background
  if _card_background is not None:
    _card_background.cancel()
    _card_background = None


async def _loop() -> None:
  from service.channels.leader import LeaderLock

  interval = max(60, int(settings.INCIDENT_SYNC_INTERVAL_SECONDS or _SYNC_INTERVAL_DEFAULT))
  lock = LeaderLock('incident:sync', ttl=interval * 2)
  while True:
    leader = True
    try:
      leader = lock.is_leader() or lock.try_acquire()
      if leader:
        lock.renew()
    except Exception as e:
      logger.warning('[Incident] 选主锁不可用，按 fail-open 继续同步: %s', e)
    if leader:
      try:
        await sync_range(int(time.time()) - interval - _OVERLAP_SECONDS)
      except Exception:
        logger.exception('[Incident] 同步失败（下一轮重试）')
    await asyncio.sleep(interval)


_background: asyncio.Task | None = None


async def start_background() -> None:
  """启动后台同步（幂等；`INCIDENT_STATS_ENABLED=false` 时不启动）。"""
  global _background
  if not settings.INCIDENT_STATS_ENABLED or _background is not None:
    return
  _background = asyncio.create_task(_loop())
  logger.info('[Incident] 通报同步任务已启动（间隔 %ss）',
              settings.INCIDENT_SYNC_INTERVAL_SECONDS)
  await start_card_refresh()


async def stop_background() -> None:
  global _background
  if _background is not None:
    _background.cancel()
    _background = None
  await stop_card_refresh()
