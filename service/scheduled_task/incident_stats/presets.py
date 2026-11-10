"""事件统计表的定时任务预设（单条 all 班次任务）。

`INCIDENT_STATS_ENABLED=true` 才建。所有已配置园区的通报**合并成一张表**，所以只建
一条 `incident__all__shift`（用户 2026-11-06 明确不要周报）。旧版按园区建的
weekly/shift 预设由 `disable_stale_system_tasks` **自动停用（不删除）**。

cron 只依赖 key（重启不抖动）：班次在 09:00 / 18:00（与不合规检查同一时点）散到
5 个分钟槽位。预设与报告预设是**两族 system 任务**，各自的 ensure 只清理自己那一族
（`disable_stale_system_tasks(prefix=…)`），不能互相停下对方。
"""

import hashlib
import logging

from config.settings import settings

logger = logging.getLogger(__name__)

_FAMILY = 'incident'
PREFIX = f'{_FAMILY}__'
PERIOD = 'weekly'
# 班次口径：与不合规检查同一时点触发（09:00 查夜班 / 18:00 查白班），期号也复用
# `2026-10-27-n` / `-d`（见 `compliance.checks.shift_window`），页面上两张卡说的是同一班次
SHIFT_PERIOD = 'shift'
# 所有园区合并成一张表：只建一条 all 班次预设，不再按园区建、不再出周报
ALL_PARKS = 'all'
_PERIODS = (SHIFT_PERIOD,)
_SHIFT_SLOTS = 5
_SLOTS = 12
_STEP_MIN = 5
_HOUR = 7


def enabled() -> bool:
  return bool(settings.INCIDENT_STATS_ENABLED)


def preset_key(park_slug: str, period: str = PERIOD) -> str:
  return f'{PREFIX}{park_slug}__{period}'


def parse_key(key: str) -> tuple[str, str] | None:
  """`incident__廊坊__weekly` → ('廊坊', 'weekly')；不是本族返回 None。"""
  parts = (key or '').split('__')
  if len(parts) != 3 or parts[0] != _FAMILY:
    return None
  return parts[1], parts[2]


def preset_cron(key: str) -> str:
  parsed = parse_key(key)
  if not parsed:
    return '0 7 * * 1'
  park, period = parsed
  digest = int(hashlib.sha1(park.encode('utf-8')).hexdigest(), 16)
  if period == SHIFT_PERIOD:
    # 5 个园区散到 09:00–09:04 / 18:00–18:04，避免同一分钟一起打 PG 与飞书接口
    return f'{digest % _SHIFT_SLOTS} 9,18 * * *'
  slot = digest % _SLOTS
  hour, minute = _HOUR + (slot * _STEP_MIN) // 60, (slot * _STEP_MIN) % 60
  return f'{minute} {hour} * * 1'


def preset_name(park_slug: str, park_name: str = '', period: str = PERIOD) -> str:
  if park_slug == ALL_PARKS:
    scope = '全部园区'
  else:
    scope = park_name or f'{park_slug}园区'
  cadence = '班次' if period == SHIFT_PERIOD else '周报'
  return f'数据中心事件统计表 · {scope} · {cadence}'


def render_prompt(key: str) -> str:
  """预设任务必填的指令字段。实际执行走确定性工作流，不消费这段文本。"""
  parsed = parse_key(key)
  park, period = parsed if parsed else ('', PERIOD)
  when = '最近一个已结束班次' if period == SHIFT_PERIOD else '上一完整自然周'
  scope = '所有已配置园区' if park == ALL_PARKS else f'{park}园区'
  return (f'生成{scope}{when}的数据中心事件统计表：'
          '以飞书园区群通报（速报 + 首报）为入口，关联告警与告警工单补全原因/进展/时长，'
          '输出 xlsx（明细 + 事件汇总）。')


async def ensure_incident_presets() -> list[str]:
  """启动时幂等建/同步事件统计表预设（多 worker 用 Redis 选主）。"""
  from service.channels.leader import LeaderLock
  from service.scheduled_task import repository as repo

  if not enabled():
    # 开关关掉也要停掉库里 enabled 的旧预设：调度器不认这个开关，看到点就会跑
    try:
      stale = await repo.disable_stale_system_tasks([], prefix=PREFIX)
      if stale:
        logger.warning('事件统计表已关闭，停用旧预设: %s', stale)
    except Exception as e:
      logger.warning('停用旧事件预设失败（不影响启动）: %s', e)
    return []

  lock = LeaderLock('sched:incident-preset-ensure', ttl=300)
  try:
    acquired = lock.try_acquire()
  except Exception as e:
    logger.warning('事件统计表预设初始化锁获取失败（继续执行）: %s', e)
    acquired = True
  if not acquired:
    logger.info('事件统计表预设已由其他 worker 初始化，跳过')
    return []

  try:
    # 计划 key 与实际建成功分开：清理 stale 必须用「计划」，否则一条建失败时
    # 另一条的成功列表会把它当已下线停掉
    planned = [preset_key(ALL_PARKS, period) for period in _PERIODS]
    created: list[str] = []
    for period in _PERIODS:
      key = preset_key(ALL_PARKS, period)
      try:
        await repo.ensure_system_task(
          key,
          name=preset_name(ALL_PARKS, period=period),
          description='数据中心事件统计表：飞书园区群通报 → 告警/工单关联，产出 xlsx',
          cron_expr=preset_cron(key),
          prompt=render_prompt(key),
          task_mode='normal',
          thread_strategy='new',
        )
        created.append(key)
      except Exception as e:
        logger.warning('事件统计表预设 %s 初始化失败（跳过）: %s', key, e)

    try:
      stale = await repo.disable_stale_system_tasks(planned, prefix=PREFIX)
      if stale:
        logger.warning('已停用下线的事件统计表预设: %s', stale)
    except Exception as e:
      logger.warning('清理下线事件预设失败（不影响启动）: %s', e)
    try:
      dup = await repo.disable_duplicate_system_tasks(planned)
      if dup:
        logger.warning('已停用 %d 条重复的事件预设: %s', len(dup), dup)
    except Exception as e:
      logger.warning('清理重复事件预设失败（不影响启动）: %s', e)
    return created
  finally:
    lock.release()
