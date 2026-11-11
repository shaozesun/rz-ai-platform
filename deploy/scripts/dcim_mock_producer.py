#!/usr/bin/env python3
"""本地 dev：模拟 DCIM 推送空间/设备/点位配置数据（无需 DCIM 环境）。

用法：
    python deploy/scripts/dcim_mock_producer.py                # 默认 localhost:9092
    python deploy/scripts/dcim_mock_producer.py --bootstrap dcim-broker-dev:9092

覆盖场景（与 docs/dcim/dcim-kafka-ingest-design.md 的验证项对应）：
   1. 首次全量：空间(园区/楼宇/房间) + 设备 + 点位
   2. 增量修改：设备改名（考察 upsert + 历史表 +1 条）
   3. 重复消息：重发同一设备（考察幂等：不产生重复行）
   4. 乱序：先推设备、后推其所属空间（考察父子颠倒不阻塞）
   5. 软删：`--with-delete` 时对某点位发删除标记（考察 op=delete）

样例字段取自《DCIM Kafka 接口文档(接入) V1.0》第 4/5/6 节。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

from confluent_kafka import Producer

TOPIC_SPACE = 'ems-config-space-rz'
TOPIC_DEVICE = 'ems-config-device-rz'
TOPIC_POINT = 'ems-config-point-rz'

IDC = 'xxxx'

SPACE_IDC = 'SPACE-IDC-0001'
SPACE_BLD = 'SPACE-BLD-0001'
SPACE_ROOM = 'SPACE-ROOM-0001'

DEVICE_A = '4e28e3e5-bd0b-4c02-ac77-9f0ab8a5c34b'
DEVICE_B = '9f1c2d3e-0000-1111-2222-333344445555'


def space(guid: str, name: str, space_type: str, parent: str) -> dict:
  return {
    'guid': guid,
    'tag': 'xxxx',
    'path': '/',
    'name': name,
    'idc': IDC,
    'node_type': 1,
    'space_type': space_type,
    'parent_id': parent,
  }


def device(guid: str, name: str, parent: str, device_type: str = '1110101') -> dict:
  return {
    'guid': guid,
    'path': f'/xxxx/{name}',
    'name': name,
    'idc': IDC,
    'node_type': 2,
    'device_type': device_type,
    'parent_id': parent,
    'extend_position': '01-13.A1',
  }


def point(dev: str, point_id: int, name: str, point_type: int, unit: str, status_map: str = '') -> dict:
  return {
    'id': point_id,
    'guid': f'{dev}.{point_id}',
    'parent_id': dev,
    'name': name,
    'idc': IDC,
    'node_type': 3,
    'point_type': point_type,
    'unit': unit,
    'status_map': status_map,
  }


def send(producer: Producer, topic: str, payload: dict, tag: str, key: str | None = None) -> None:
  """按实体 guid 作为消息 key：同一实体的消息落同一分区，保证消费有序。

  （生产侧 DCIM 也应按此约定推送，否则"删除/更新"可能与"新增"乱序，
   导致删除被后续 upsert 覆盖。）
  """
  producer.produce(
    topic,
    key=(key or payload.get('guid') or '').encode('utf-8'),
    value=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
  )
  producer.poll(0)
  print(f'  → [{topic}] {tag}: {payload.get("name") or payload.get("guid")}')


def main() -> int:
  ap = argparse.ArgumentParser()
  ap.add_argument('--bootstrap', default='localhost:9092')
  ap.add_argument('--with-delete', action='store_true', help='额外推送一条点位软删标记')
  ap.add_argument('--security-protocol', default=os.environ.get('KAFKA_SECURITY_PROTOCOL', 'PLAINTEXT'),
                  help='PLAINTEXT / SASL_PLAINTEXT')
  ap.add_argument('--sasl-username', default=os.environ.get('KAFKA_SASL_USERNAME', 'sspaas'))
  ap.add_argument('--sasl-password', default=os.environ.get('KAFKA_SASL_PASSWORD', 'sspaas#123'))
  args = ap.parse_args()

  conf: dict = {'bootstrap.servers': args.bootstrap}
  if args.security_protocol.upper().startswith('SASL'):
    conf.update({
      'security.protocol': args.security_protocol,
      'sasl.mechanism': 'PLAIN',
      'sasl.username': args.sasl_username,
      'sasl.password': args.sasl_password,
    })
  producer = Producer(conf)

  print('1) 首次全量：空间（数据中心 → 楼宇 → 房间）')
  send(producer, TOPIC_SPACE, space(SPACE_IDC, 'xx 园区 A18 数据中心', 'IDC', ''), '根')
  send(producer, TOPIC_SPACE, space(SPACE_BLD, 'A18 楼宇', 'BLD', SPACE_IDC), '楼宇')
  send(producer, TOPIC_SPACE, space(SPACE_ROOM, 'A18-201 包间', 'ROOM', SPACE_BLD), '房间')

  print('2) 首次全量：设备 + 点位')
  send(producer, TOPIC_DEVICE, device(DEVICE_A, 'F-13-01-A路', SPACE_ROOM), '设备A')
  send(producer, TOPIC_POINT, point(DEVICE_A, 1001000, '第15节电压', 1, 'V'), '点位(电压)')
  send(producer, TOPIC_POINT, point(DEVICE_A, 1001001, '通讯状态', 3, '', '1=正常;0=中断'), '点位(通讯状态)')

  print('3) 增量修改：设备改名（考察 upsert + 历史表新增一条）')
  send(producer, TOPIC_DEVICE, device(DEVICE_A, 'F-13-01-A路(改名)', SPACE_ROOM), '改名')

  print('4) 重复消息：重发同一设备（考察幂等，不应产生重复行）')
  send(producer, TOPIC_DEVICE, device(DEVICE_A, 'F-13-01-A路(改名)', SPACE_ROOM), '重复')

  print('5) 乱序：先推设备B，后推其所属空间（考察父子颠倒不阻塞）')
  send(producer, TOPIC_DEVICE, device(DEVICE_B, 'F-13-02-B路', 'SPACE-ROOM-9999'), '设备B(空间未到)')
  time.sleep(0.3)
  send(producer, TOPIC_SPACE, space('SPACE-ROOM-9999', 'A18-202 包间', 'ROOM', SPACE_BLD), '迟到的空间')

  if args.with_delete:
    print('6) 软删：点位 1001001 标记删除（考察 op=delete）')
    send(producer, TOPIC_POINT, {'guid': f'{DEVICE_A}.1001001', 'deleted': True}, '删除标记')

  producer.flush(10)
  print('完成：已推送全部样例数据')
  return 0


if __name__ == '__main__':
  sys.exit(main())
