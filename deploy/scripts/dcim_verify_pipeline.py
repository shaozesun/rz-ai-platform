#!/usr/bin/env python3
"""本地 dev：不依赖 Kafka，直接验证 DCIM 落库流水线（解析 → 当前态 + 历史 → 查询）。

用途：Kafka 环境未就绪时先验证 PG 侧逻辑；也可作为回归脚本。
    python deploy/scripts/dcim_verify_pipeline.py --dsn postgresql://dcim:dcim@localhost:5433/dcim

覆盖：首次全量落库 / 增量修改（历史 +1）/ 重复消息（幂等）/ 乱序（设备先于空间）/ 软删 / 递归 CTE 查树。
"""

from __future__ import annotations

import argparse
import itertools
import sys

from service.dcim_ingest.models import parse_device, parse_point, parse_space
from service.dcim_ingest.repository import DcimRepository
from service.dcim_ingest.schema import ensure_schema

CLUSTER = '廊坊A'   # 园区维度：cluster 由消费端从「消息来自哪个集群」注入
_offsets = itertools.count(1)


def meta(cluster: str = CLUSTER) -> dict:
  """每条消息一个**独立递增的 offset**。

  真实 Kafka 里 offset 是单调递增的、天然唯一；告警历史表按
  `(cluster, kafka_topic, partition, "offset")` 做幂等约束，所以脚本必须模拟这一点 ——
  否则同一 offset 的第二条会被 `ON CONFLICT DO NOTHING` 拦住（那是**正确**行为，是脚本不真实）。
  """
  return {'cluster': cluster, 'topic': 'local-verify', 'partition': 0, 'offset': next(_offsets)}


def main() -> int:
  ap = argparse.ArgumentParser()
  ap.add_argument('--dsn', default='postgresql://dcim:dcim@localhost:5433/dcim')
  args = ap.parse_args()

  # 幂等建表（新库可直接跑）。注意：**已存在旧结构的库不会被改**，
  # 那种情况需先 DROP 这 4 张表再跑（阶段一改造时生产库即如此，见部署手册）。
  ensure_schema(args.dsn)

  repo = DcimRepository(args.dsn)
  repo.connect()
  ok = True

  def check(name: str, got, want) -> None:
    nonlocal ok
    flag = 'OK  ' if got == want else 'FAIL'
    if got != want:
      ok = False
    print(f'{flag} {name}: got={got} want={want}')

  # 0) 清库（保证可重复执行）
  with repo.conn.cursor() as cur:
    for t in ('dcim_spaces', 'dcim_devices', 'dcim_points', 'dcim_dlq'):
      cur.execute(f'TRUNCATE TABLE "{t}" RESTART IDENTITY')
  repo.conn.commit()

  # 1) 首次全量
  repo.apply_batch('space', [
    (parse_space({'guid': 'S-IDC', 'name': 'A18 数据中心', 'idc': 'x', 'node_type': 1, 'space_type': 'IDC', 'parent_id': ''}), meta()),
    (parse_space({'guid': 'S-BLD', 'name': 'A18 楼宇', 'idc': 'x', 'node_type': 1, 'space_type': 'BLD', 'parent_id': 'S-IDC'}), meta()),
    (parse_space({'guid': 'S-ROOM', 'name': 'A18-201 包间', 'idc': 'x', 'node_type': 1, 'space_type': 'ROOM', 'parent_id': 'S-BLD'}), meta()),
  ])
  repo.apply_batch('device', [
    (parse_device({'guid': 'D-A', 'name': 'F-13-01-A路', 'idc': 'x', 'node_type': 2,
                   'device_type': '1110101', 'device_name': '精密空调',
                   'parent_id': 'S-ROOM'}), meta()),
  ])
  repo.apply_batch('point', [
    (parse_point({'id': 1001000, 'guid': 'D-A.1001000', 'parent_id': 'D-A', 'name': '第15节电压',
                  'idc': 'x', 'node_type': 3, 'point_type': 1, 'unit': 'V',
                  'urgent_alarm': {'maxUse': 1, 'minUse': 0, 'maxAlarm': 240.0,
                                   'minAlarm': 180.0, 'maxEvent': '电压过高', 'minEvent': ''}}), meta()),
    (parse_point({'id': 1001001, 'guid': 'D-A.1001001', 'parent_id': 'D-A', 'name': '通讯状态',
                  'idc': 'x', 'node_type': 3, 'point_type': 3,
                  'status_map': '1=正常;0=中断'}), meta()),
  ])
  check('空间数', repo.count('dcim_spaces'), 3)
  check('设备数', repo.count('dcim_devices'), 1)
  check('点位数', repo.count('dcim_points'), 2)

  # 1b) 9/29 版新增字段：device_name（设备类型名称）与点位告警阈值
  with repo.conn.cursor() as cur:
    cur.execute("SELECT device_name, name FROM dcim_devices WHERE guid = 'D-A'")
    row = cur.fetchone()
    check('device_name = 类型名称', row['device_name'], '精密空调')
    check('name 不受影响', row['name'], 'F-13-01-A路')
    cur.execute("SELECT urgent_alarm FROM dcim_points WHERE guid = 'D-A.1001000'")
    check('urgent_alarm 阈值落库', cur.fetchone()['urgent_alarm'].get('maxAlarm'), 240.0)
    cur.execute("SELECT hint_alarm FROM dcim_points WHERE guid = 'D-A.1001000'")
    check('未提供的阈值字段为 NULL', cur.fetchone()['hint_alarm'], None)

  # 2) 增量修改（改名）→ 当前态就地更新，不新增行
  repo.apply_batch('device', [
    (parse_device({'guid': 'D-A', 'name': 'F-13-01-A路(改名)', 'idc': 'x', 'node_type': 2, 'device_type': '1110101', 'parent_id': 'S-ROOM'}), meta()),
  ])
  check('改名后设备数(不新增)', repo.count('dcim_devices'), 1)

  # 3) 重复消息（幂等）
  repo.apply_batch('device', [
    (parse_device({'guid': 'D-A', 'name': 'F-13-01-A路(改名)', 'idc': 'x', 'node_type': 2, 'device_type': '1110101', 'parent_id': 'S-ROOM'}), meta()),
  ])
  check('重复后设备数(幂等)', repo.count('dcim_devices'), 1)

  # 4) 乱序：设备先于其空间到达
  repo.apply_batch('device', [
    (parse_device({'guid': 'D-B', 'name': 'F-13-02-B路', 'idc': 'x', 'node_type': 2, 'device_type': '1110101', 'parent_id': 'S-ROOM-9999'}), meta()),
  ])
  check('乱序设备已落库', repo.count('dcim_devices'), 2)
  repo.apply_batch('space', [
    (parse_space({'guid': 'S-ROOM-9999', 'name': 'A18-202 包间', 'idc': 'x', 'node_type': 1, 'space_type': 'ROOM', 'parent_id': 'S-BLD'}), meta()),
  ])
  with repo.conn.cursor() as cur:
    cur.execute(
      'SELECT count(*) AS n FROM dcim_devices d JOIN dcim_spaces s'
      ' ON d.space_guid = s.guid AND d.cluster = s.cluster'
    )
    check('乱序后父子可关联', cur.fetchone()['n'], 2)

  # 5) 软删
  repo.apply_batch('point', [
    (parse_point({'guid': 'D-A.1001001', 'deleted': True}), meta()),
  ])
  with repo.conn.cursor() as cur:
    cur.execute("SELECT is_deleted FROM dcim_points WHERE guid = 'D-A.1001001'")
    check('点位软删标记', cur.fetchone()['is_deleted'], True)

  # 6) 递归 CTE 查空间树（按园区隔离）
  tree = repo.space_tree(CLUSTER, 'S-IDC')
  names = [n['name'] for n in tree]
  check('空间树节点数', len(names), 4)
  print('    空间树:', ' → '.join(names))

  # 7) 点位字典（单位 + 状态映射）
  with repo.conn.cursor() as cur:
    cur.execute("SELECT name, unit, status_map FROM dcim_points WHERE guid = 'D-A.1001001'")
    row = cur.fetchone()
    check('点位状态映射解析', row['status_map'], {'1': '正常', '0': '中断'})

  # 8) 跨园区同 guid：复合主键必须让两条并存
  #    （单列 guid 主键时，后到的园区会静默覆盖前一个 —— 这是本次改造要防的数据丢失）
  meta_b = meta('重庆')
  repo.apply_batch('device', [
    (parse_device({'guid': 'D-A', 'name': '重庆同 guid 设备', 'idc': 'x', 'node_type': 2,
                   'device_type': '1110101', 'parent_id': 'S-ROOM'}), meta_b),
  ])
  check('跨园区同 guid 并存', repo.count('dcim_devices'), 3)
  check('廊坊A 的 D-A 未被覆盖', repo.count('dcim_devices', CLUSTER), 2)
  check('重庆 的 D-A 已落库', repo.count('dcim_devices', '重庆'), 1)

  repo.close()
  print('\n结果:', '全部通过 ✅' if ok else '存在失败 ❌')
  return 0 if ok else 1


if __name__ == '__main__':
  sys.exit(main())
