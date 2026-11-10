"""飞书群消息**实时入库**（WS 推送 → `dcim_incident_reports`）。

与 `sync.py` 的分工：`sync` 是**拉**（首次回填 / 断线补偿），本模块是**推**（主路径）
—— 群里任何人发消息，WS 事件到达即入库，不再等 300s 轮询。

三条边界（刻意为之）：
1. **只入库，不进对话**：完全不碰 `runner` / `AgentService`。同一群里 @ 机器人的
   消息照旧走 `channel._on_message` 的原逻辑，两条路并存、互不影响。
2. **白名单 fail-closed**：`FEISHU_GROUP_INGEST_CHATS` 为空 → 一律不入库。群消息读
   权限（`im:message.group_msg`）是敏感权限，不提供「默认全收」。开关也独立于
   `INCIDENT_STATS_ENABLED`（那个还会建事件统计表的定时预设）。
3. **只认速报/首报触发匹配**：`other` 也入库（统计口径不变），但只有 sos/first 才
   触发告警匹配。

行构造复用 `feishu.build_report_row`，与拉取路径**完全同形**（字段/派生规则不分叉）。
"""

import asyncio
import logging
import time

from config.settings import settings
from service.scheduled_task.incident_stats import extract, feishu, schema, source, sync

logger = logging.getLogger(__name__)

_DEBOUNCE_SECONDS = 2         # 攒一小批再匹配（Kafka 告警常比群消息晚到几秒）
_MATCH_LIMIT = 100            # 单次实时匹配上限（剩下的留给下一轮/补偿循环）
_CLUSTER_TTL_SECONDS = 300    # 园区群映射的进程内缓存时长（群配置改了 5 分钟内生效）

_match_task: asyncio.Task | None = None
_backfill_task: asyncio.Task | None = None
_cluster_cache: dict[str, str] = {}
_cluster_loaded_at = 0.0
_name_cache: dict[str, str] = {}
_name_miss: set[str] = set()


def ingest_chat_ids() -> list[str]:
  """`FEISHU_GROUP_INGEST_CHATS` → chat_id 列表（逗号分隔，去空去重保序）。"""
  raw = settings.FEISHU_GROUP_INGEST_CHATS or ''
  return list(dict.fromkeys(
    part.strip() for part in raw.replace('，', ',').split(',') if part.strip()
  ))


def should_ingest(chat_id: str) -> bool:
  """是否入库：开关 + 白名单（纯函数，便于离线断言）。"""
  if not settings.FEISHU_GROUP_INGEST_ENABLED:
    return False
  chat_id = (chat_id or '').strip()
  return bool(chat_id) and chat_id in ingest_chat_ids()


def on_message_event(event) -> None:
  """WS 线程内调用：只做轻量取值，其余交回主 loop。绝不抛。"""
  from service.feishu.client import submit_async

  try:
    message = getattr(getattr(event, 'event', None), 'message', None)
    chat_id = getattr(message, 'chat_id', '') or ''
    if not should_ingest(chat_id):
      return
    sender = getattr(getattr(event.event, 'sender', None), 'sender_id', None)
    submit_async(_ingest_one(
      chat_id=chat_id,
      message_id=getattr(message, 'message_id', '') or '',
      create_s=feishu._to_seconds(getattr(message, 'create_time', None)),
      open_id=(getattr(sender, 'open_id', '') or getattr(sender, 'user_id', '') or ''),
      msg_type=getattr(message, 'message_type', '') or '',
      content=getattr(message, 'content', '') or '',
    ))
  except Exception:
    logger.exception('[Incident] 实时入库事件解析失败')


def on_recall_event(chat_id: str, message_id: str) -> None:
  """WS 线程内调用：撤回 → 物理删行（当没发过）。只做轻量取值，其余交回主 loop。绝不抛。"""
  from service.feishu.client import submit_async

  try:
    message_id = (message_id or '').strip()
    if not message_id or not should_ingest(chat_id):
      return
    submit_async(_delete_one(chat_id=chat_id, message_id=message_id))
  except Exception:
    logger.exception('[Incident] 撤回事件解析失败')


async def _delete_one(*, chat_id: str, message_id: str) -> None:
  """删掉被撤回的那一行。删 0 行也正常（消息可能本就不在库里）。"""
  try:
    deleted = await asyncio.to_thread(source.delete_report, chat_id, message_id)
  except Exception as e:
    logger.warning('[Incident] 删除撤回消息失败 chat=%s msg=%s: %s', chat_id, message_id, e)
    return
  logger.info('[Incident] 撤回消息已删除 chat=%s msg=%s rows=%d', chat_id, message_id, deleted)


async def _ingest_one(*, chat_id: str, message_id: str, create_s: int | None,
                      open_id: str, msg_type: str, content: str) -> None:
  """一条 WS 消息 → 一行入库；只有 sos/first 触发去抖匹配。"""
  if not message_id or create_s is None:
    return
  text = feishu.text_from_content(msg_type, content).strip()
  if not text:
    logger.debug('[Incident] 实时消息无文本，跳过 chat=%s msg=%s type=%s',
                 chat_id, message_id, msg_type)
    return
  row = feishu.build_report_row(
    chat_id=chat_id,
    message_id=message_id,
    create_s=create_s,
    sender=await _sender_name(open_id),
    sender_id=open_id,
    sender_id_type='open_id' if open_id else '',
    msg_type=msg_type,
    text=text,
    raw_body=content,
  )
  # 园区（= cluster）：匹配与统计都按它分组，映射取自 INCIDENT_FEISHU_PARK_CHATS
  row['cluster'] = await _cluster_of(chat_id) or None
  # 确定性解析没拿到发生时间/告警值时让模型补一次（单条上限 1 次，失败照常入库）
  await extract.enrich_rows([row], max_calls=1)
  try:
    await asyncio.to_thread(source.save_reports, [row])
  except Exception as e:
    logger.warning('[Incident] 实时入库失败 chat=%s msg=%s: %s', chat_id, message_id, e)
    return
  logger.info('[Incident] 实时入库 chat=%s msg=%s kind=%s cluster=%s',
              chat_id, message_id, row['kind'], row['cluster'] or '-')
  if row['kind'] in ('sos', 'first'):
    _schedule_match()


async def _sender_name(open_id: str) -> str:
  """open_id → 姓名（通讯录，进程内缓存）。

  取不到就留空：`save_reports` 用 `COALESCE(NULLIF(...))` 保护，不会把回补时拉到的
  真名冲掉。缺 `contact:contact.base:readonly` 权限时只 warn 一次，不刷日志。
  """
  if not open_id:
    return ''
  if open_id in _name_cache:
    return _name_cache[open_id]
  if open_id in _name_miss:
    return ''
  from service.feishu import contact

  try:
    names = await contact.resolve_names([open_id])
  except Exception as e:
    logger.warning('[Incident] 解析发送人姓名失败 open_id=%s: %s', open_id, e)
    _name_miss.add(open_id)
    return ''
  name = names.get(open_id, '')
  if name:
    _name_cache[open_id] = name
  else:
    _name_miss.add(open_id)
    logger.warning('[Incident] 通讯录查不到发送人姓名 open_id=%s（缺权限或已离职）', open_id)
  return name


async def _refresh_cluster_cache() -> None:
  """刷新 {chat_id: 园区}（`INCIDENT_FEISHU_PARK_CHATS` 的 {园区: chat_id} 取反）。"""
  global _cluster_cache, _cluster_loaded_at
  try:
    mapping = await feishu.resolve_park_chats()
  except Exception as e:
    logger.warning('[Incident] 解析园区群失败（cluster 留空）: %s', e)
    mapping = {}
  _cluster_cache = {cid: park for park, cid in mapping.items()}
  _cluster_loaded_at = time.time()


async def _cluster_of(chat_id: str) -> str:
  """chat_id → 园区名；取不到返回空串。

  留空的行照入库，但配不上告警（`sync.match_one` 需要 cluster）——启动时会对
  「在入库白名单里、却没有园区映射」的群打 warn。
  """
  if time.time() - _cluster_loaded_at > _CLUSTER_TTL_SECONDS:
    await _refresh_cluster_cache()
  return _cluster_cache.get(chat_id, '')


def _schedule_match() -> None:
  """去抖触发匹配：窗口内连续到达只跑一次（已在跑则不再叠加）。"""
  global _match_task
  if _match_task is not None and not _match_task.done():
    return
  _match_task = asyncio.create_task(_debounced_match())


async def _debounced_match() -> None:
  try:
    await asyncio.sleep(_DEBOUNCE_SECONDS)
    matched = await sync.match_pending(_MATCH_LIMIT)
    if matched:
      logger.info('[Incident] 实时匹配成功 %d 条', matched)
  except asyncio.CancelledError:
    raise
  except Exception:
    logger.exception('[Incident] 实时匹配失败（补偿循环会重试）')


async def _backfill_loop() -> None:
  """低频补偿：每 N 分钟按水位补一次缺口（不是固定滑窗，见 `sync.sync_gaps`）。"""
  from service.channels.leader import LeaderLock

  window = max(5, int(settings.FEISHU_GROUP_INGEST_BACKFILL_MINUTES or 30))
  interval = window * 60
  cap_seconds = max(
    interval, int(settings.FEISHU_GROUP_INGEST_MAX_BACKFILL_HOURS or 72) * 3600,
  )
  # 与 `incident:sync`（INCIDENT_STATS_ENABLED 的轮询）分开选主，互不争锁
  lock = LeaderLock('incident:backfill', ttl=interval * 2)
  while True:
    leader = True
    try:
      leader = lock.is_leader() or lock.try_acquire()
      if leader:
        lock.renew()
    except Exception as e:
      logger.warning('[Incident] 回补选主锁不可用，按 fail-open 继续: %s', e)
    if leader:
      try:
        await sync.sync_gaps(cap_seconds)
      except Exception:
        logger.exception('[Incident] 实时回补失败（下一轮重试）')
    await asyncio.sleep(interval)


async def start() -> None:
  """启动实时入库（幂等）。WS 侧由 `LARK_ENABLED` 负责，这里只管落库与补偿。"""
  global _backfill_task
  if not settings.FEISHU_GROUP_INGEST_ENABLED:
    return
  if not settings.LARK_ENABLED:
    logger.warning('[Incident] 实时入库需要 LARK_ENABLED=true（WS 是唯一入口），已跳过')
    return
  chats = ingest_chat_ids()
  if not chats:
    logger.warning('[Incident] FEISHU_GROUP_INGEST_CHATS 为空，实时入库 fail-closed 不生效')
    return
  try:
    await asyncio.to_thread(schema.ensure_reports_schema, settings.DCIM_PG_DSN)
  except Exception as e:
    # 表多半已存在（同步/CLI 建过），不因此拒绝启动
    logger.warning('[Incident] 实时入库建表检查失败: %s', e)

  await _refresh_cluster_cache()
  missing = [c for c in chats if c not in _cluster_cache]
  if missing:
    logger.warning('[Incident] 以下入库白名单群没有园区映射（INCIDENT_FEISHU_PARK_CHATS），'
                   '能入库但配不上告警: %s', missing)
  outside = [c for c in _cluster_cache if c not in chats]
  if outside:
    logger.warning('[Incident] 园区映射里的群不在入库白名单（FEISHU_GROUP_INGEST_CHATS），'
                   '收不到实时推送、只能靠 %d 分钟回补: %s',
                   settings.FEISHU_GROUP_INGEST_BACKFILL_MINUTES, outside)
  # 补偿循环按园区群整段重拉，没有园区配置就没有可补的窗口
  if (settings.INCIDENT_FEISHU_PARK_CHATS or '').strip():
    if _backfill_task is None or _backfill_task.done():
      _backfill_task = asyncio.create_task(_backfill_loop())
  else:
    logger.warning('[Incident] 未配置 INCIDENT_FEISHU_PARK_CHATS，跳过断线补偿循环')
  # 实时入库开启而 INCIDENT_STATS_ENABLED 关闭时，由这里启动卡片进展回捞（幂等）
  await sync.start_card_refresh()
  logger.info('[Incident] 飞书群实时入库已启用：%d 个群，补偿窗口 %d 分钟',
              len(chats), settings.FEISHU_GROUP_INGEST_BACKFILL_MINUTES)


async def stop() -> None:
  """停掉补偿循环与去抖匹配（测试/关停用；正常进程退出由 loop 关闭接管）。"""
  global _backfill_task, _match_task
  for task in (_backfill_task, _match_task):
    if task is not None and not task.done():
      task.cancel()
      try:
        await task
      except asyncio.CancelledError:
        pass
  _backfill_task = None
  _match_task = None
  await sync.stop_card_refresh()
