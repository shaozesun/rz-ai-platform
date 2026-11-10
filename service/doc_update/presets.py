"""文档更新检查的定时任务预设（每月 10 日 03:00 跑一期）。

**默认关闭**（`DOC_UPDATE_CHECK_ENABLED=false`）：`ensure_doc_update_presets()`
直接返回空，不建任务、不跑 cron，页面与工具都不出现。

预设是**独立一族** system 任务（前缀 `doc_update__`）：`report_templates` 的
stale 清理必须排除本前缀，否则启动时会被当成下线预设停用。
"""

import logging

from config.settings import settings

logger = logging.getLogger(__name__)

PREFIX = 'doc_update__'
KEY = f'{PREFIX}monthly'
NAME = '平台每月所需更新文件目录 · 各部门月度文档更新检查'


def enabled() -> bool:
  return bool(settings.DOC_UPDATE_CHECK_ENABLED)


def is_preset_key(key: str) -> bool:
  return (key or '').startswith(PREFIX)


def preset_cron(_key: str = KEY) -> str:
  """只依赖固定时刻（每月 10 日 03:00），ensure 幂等、重启不抖动。"""
  return settings.DOC_UPDATE_CRON or '0 3 10 * *'


def render_prompt(_key: str = KEY) -> str:
  """预设任务必填的指令字段。实际执行走确定性工作流，不消费这段文本。"""
  return ('逐部门（A1…A18）在共享文档目录下按 25 条月度清单项取快照：'
          '有月份目录的看本期应到月份有没有文件，无月份目录的比对 sha256；'
          '输出已更新 / 未更新 / 未找到 / 下载失败四项计数。')


async def ensure_doc_update_presets() -> list[str]:
  """启动时幂等建/同步文档更新检查预设（多 worker 用 Redis 选主）。"""
  if not enabled():
    return []
  from service.channels.leader import LeaderLock
  from service.scheduled_task import repository as repo

  lock = LeaderLock('sched:doc-update-preset-ensure', ttl=300)
  try:
    acquired = lock.try_acquire()
  except Exception as e:
    logger.warning('文档更新检查预设初始化锁获取失败（继续执行）: %s', e)
    acquired = True
  if not acquired:
    logger.info('文档更新检查预设已由其他 worker 初始化，跳过')
    return []

  try:
    try:
      await repo.ensure_system_task(
        KEY,
        name=NAME,
        description='各部门月度文档更新检查：25 条月度项 × A1…A18 部门',
        cron_expr=preset_cron(KEY),
        prompt=render_prompt(KEY),
        task_mode='normal',
        thread_strategy='new',
      )
    except Exception as e:
      logger.warning('文档更新检查预设初始化失败（跳过）: %s', e)
      return []

    try:
      stale = await repo.disable_stale_system_tasks([KEY], prefix=PREFIX)
      if stale:
        logger.warning('已停用下线的文档更新检查预设: %s', stale)
    except Exception as e:
      logger.warning('清理下线文档更新检查预设失败（不影响启动）: %s', e)
    try:
      dup = await repo.disable_duplicate_system_tasks([KEY])
      if dup:
        logger.warning('已停用 %d 条重复的文档更新检查预设: %s', len(dup), dup)
    except Exception as e:
      logger.warning('清理重复文档更新检查预设失败（不影响启动）: %s', e)
    return [KEY]
  finally:
    lock.release()
