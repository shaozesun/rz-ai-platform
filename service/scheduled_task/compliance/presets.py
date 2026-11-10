"""不合规检查的定时任务预设（班次：09:00 查夜班 / 18:00 查白班）。

**默认关闭**（`COMPLIANCE_ENABLED=false`）：`ensure_compliance_presets()` 直接返回空，
不建任务、不跑 cron，页面上只有空状态。开启后每班次产出一份 xlsx。

预设与「分析报告」「事件统计表」是**三族 system 任务**（前缀 `compliance__`）：
各自的 ensure 只清理自己那一族，`report_templates` 的 stale 清理必须排除本前缀。
"""

import logging

from config.settings import settings

logger = logging.getLogger(__name__)

PREFIX = 'compliance__'
KEY = f'{PREFIX}alarm_shift'
NAME = '运营监控中心告警不合规检查 · 班次'
CRON = '0 9,18 * * *'


def enabled() -> bool:
  return bool(settings.COMPLIANCE_ENABLED)


def is_preset_key(key: str) -> bool:
  return (key or '').startswith(PREFIX)


def preset_cron(_key: str = KEY) -> str:
  """只依赖固定时刻（09:00 夜班 / 18:00 白班），ensure 幂等、重启不抖动。"""
  return CRON


def render_prompt(_key: str = KEY) -> str:
  """预设任务必填的指令字段。实际执行走确定性工作流，不消费这段文本。"""
  return ('产出上一班次的运营监控中心告警不合规检查表：告警响应超时 / 告警未关单 / '
          '告警未解除关单 / 重复告警，输出一份 xlsx（一个检查项一个 sheet）。')


async def ensure_compliance_presets() -> list[str]:
  """启动时幂等建/同步不合规检查预设（多 worker 用 Redis 选主）。"""
  if not enabled():
    return []
  from service.channels.leader import LeaderLock
  from service.scheduled_task import repository as repo

  lock = LeaderLock('sched:compliance-preset-ensure', ttl=300)
  try:
    acquired = lock.try_acquire()
  except Exception as e:
    logger.warning('不合规检查预设初始化锁获取失败（继续执行）: %s', e)
    acquired = True
  if not acquired:
    logger.info('不合规检查预设已由其他 worker 初始化，跳过')
    return []

  try:
    try:
      await repo.ensure_system_task(
        KEY,
        name=NAME,
        description='运营监控中心告警不合规检查：按班次核对四项告警处置口径，产出 xlsx',
        cron_expr=preset_cron(KEY),
        prompt=render_prompt(KEY),
        task_mode='normal',
        thread_strategy='new',
      )
    except Exception as e:
      logger.warning('不合规检查预设初始化失败（跳过）: %s', e)
      return []

    try:
      stale = await repo.disable_stale_system_tasks([KEY], prefix=PREFIX)
      if stale:
        logger.warning('已停用下线的不合规检查预设: %s', stale)
    except Exception as e:
      logger.warning('清理下线不合规检查预设失败（不影响启动）: %s', e)
    try:
      dup = await repo.disable_duplicate_system_tasks([KEY])
      if dup:
        logger.warning('已停用 %d 条重复的不合规检查预设: %s', len(dup), dup)
    except Exception as e:
      logger.warning('清理重复不合规检查预设失败（不影响启动）: %s', e)
    return [KEY]
  finally:
    lock.release()
