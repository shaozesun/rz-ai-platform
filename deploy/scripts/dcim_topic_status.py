#!/usr/bin/env python3
"""探测各园区集群上 DCIM 的 `-rz` topic 是否已创建（**只看元数据，不消费、不提交 offset**）。

用途：全量接入前后各跑一次 —— 对方说"topic 建好了"，用这个验而不是听。
    cd /opt/rz-ai
    docker run --rm --env-file deploy/config/.env.production -v "$PWD:/app" -w /app \
      rz-ai-backend:latest python deploy/scripts/dcim_topic_status.py

退出码：全部园区的 topic 都在 → 0；有缺 → 1（可直接用于巡检/告警）。
"""

from __future__ import annotations

import sys
from pathlib import Path

# 直接 `python deploy/scripts/dcim_topic_status.py` 时 sys.path[0] 是**脚本所在目录**，
# `import config` 会 ModuleNotFoundError → 显式把仓库根加进来（本文件在 <root>/deploy/scripts/）。
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from confluent_kafka.admin import AdminClient  # noqa: E402

from config.settings import settings  # noqa: E402
from service.dcim_ingest.config import clusters, consumer_config  # noqa: E402


# consumer 专属项：AdminClient 拿到这些会**每条都刷一遍** CONFWARN 到 stderr，把表格淹掉
_CONSUMER_ONLY = ('group.id', 'enable.auto.commit', 'auto.offset.reset', 'enable.partition.eof')


def admin_config(cluster) -> dict:
  """复用 `consumer_config`（省得重写 bootstrap/SASL），只把 consumer 专属项剔掉。"""
  conf = consumer_config(cluster)
  for key in _CONSUMER_ONLY:
    conf.pop(key, None)
  return conf


def expected_topics() -> list[tuple[str, str]]:
  """[(简称, 真实 topic 名)]：我方要消费的全部 topic。名字从 settings 取，不硬编码。"""
  return [
    ('space', settings.KAFKA_TOPIC_SPACE),
    ('device', settings.KAFKA_TOPIC_DEVICE),
    ('point', settings.KAFKA_TOPIC_POINT),
    ('real', settings.KAFKA_TOPIC_REAL_DATA),
    ('alarm', settings.KAFKA_TOPIC_ALARM),
    ('order', settings.KAFKA_TOPIC_ALARM_ORDER),
  ]


def _width(s: str) -> int:
  """显示宽度：中日韩字符占 2 列，按 len() 补齐会让表格歪掉。"""
  return sum(2 if ord(ch) > 0x2E7F else 1 for ch in s)


def _pad(s: str, width: int) -> str:
  return s + ' ' * max(0, width - _width(s))


def probe() -> list[tuple[str, list[bool], str]]:
  """逐个集群探测。连不上只判该园区为缺，不中断其余（单点中转抖动很常见）。"""
  wanted = expected_topics()
  rows: list[tuple[str, list[bool], str]] = []
  for c in clusters():
    try:
      meta = AdminClient(admin_config(c)).list_topics(timeout=15)
      present = {t for t in meta.topics if not t.startswith('__')}
      rows.append((c.cluster, [t in present for _, t in wanted], f'{len(meta.brokers)} broker'))
    except Exception as e:  # noqa: BLE001 —— 探测失败就是"没就绪"，不该让整条命令挂掉
      rows.append((c.cluster, [False] * len(wanted), f'{type(e).__name__}: {str(e)[:44]}'))
  return rows


def main() -> int:
  wanted = expected_topics()
  rows = probe()

  head = _pad('园区', 12) + '  '.join(_pad(n, 8) for n, _ in wanted) + '备注'
  print(head)
  print('-' * _width(head))
  for cluster, marks, note in rows:
    cells = '  '.join(_pad('✓' if m else '-', 8) for m in marks)
    print(_pad(cluster, 12) + cells + note)

  ready = sum(1 for _, marks, _ in rows if all(marks))
  total = len(rows)
  print()
  print(f'就绪园区 {ready}/{total}（判据：上表 {len(wanted)} 个 topic 全在）')
  if not settings.DCIM_REALTIME_ENABLED:
    print('注：DCIM_REALTIME_ENABLED=false —— real 那一列仅作参考，我方暂不消费')
  if not settings.DCIM_ALARM_ENABLED:
    print('注：DCIM_ALARM_ENABLED=false —— alarm 那一列仅作参考，我方暂不消费')
  if not settings.DCIM_ALARM_ORDER_ENABLED:
    print('注：DCIM_ALARM_ORDER_ENABLED=false —— order 那一列仅作参考，我方暂不消费')

  if ready == total:
    print('全部就绪。topic 建好后 worker **无需重启**（订阅已在，librdkafka 周期刷新元数据）。')
    return 0

  not_ready = [c for c, marks, _ in rows if not all(marks)]
  print('尚未就绪：' + '、'.join(not_ready))
  print('提示：对方建好 topic 后 worker 会自动开始消费（无需重启），'
        '但在那之前日志里会周期刷 UNKNOWN_TOPIC_OR_PART，属预期，不是故障。')
  return 1


if __name__ == '__main__':
  sys.exit(main())
