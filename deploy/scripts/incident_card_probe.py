#!/usr/bin/env python3
"""事件统计表：**卡片（interactive）速报**入库实测。

现场有一种卡片式通报，用户自己发不出来，只能在群里由机器人推一张模拟卡片来验证
「卡片 → `_walk_texts` 抽文本 → classify/发生时间/告警值」这条链路。卡片体用最小
markdown 卡，与真实卡片走**同一份**解析代码（`feishu.text_from_content`）。

    python deploy/scripts/incident_card_probe.py                      # 离线断言，不联网
    python deploy/scripts/incident_card_probe.py --send               # 发到群里
    python deploy/scripts/incident_card_probe.py --check --minutes 10 # 拉取路径核对
    python deploy/scripts/incident_card_probe.py --recall om_xxx      # 撤回（会连带删行）

注意：机器人**自己发的消息不会经 WS 推回**，入库只能靠拉取路径（30 分钟补偿循环，
或平台查询工具默认的 `sync=True`）。本脚本不连库、不写库。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

try:
  ROOT = Path(__file__).resolve().parents[2]
except (NameError, IndexError):
  ROOT = Path.cwd()
if str(ROOT) not in sys.path:
  sys.path.insert(0, str(ROOT))

from config.settings import settings  # noqa: E402
from service.scheduled_task.incident_stats import feishu  # noqa: E402

_TZ = ZoneInfo('Asia/Shanghai')
# 本地 .env 通常没有园区映射，留一个兜底（生产 `INCIDENT_FEISHU_PARK_CHATS` 的廊坊群）
_FALLBACK_CHAT_ID = 'oc_451f6cd50979edad1fd260bcf19fbf5f'
_PARK = '廊坊'
_ALARM_VALUE = 99999
_MARK = '卡片解析自检'      # 【描述】里的标记：`--check` 靠它把测试卡片捞出来


def _chat_id() -> str:
  """默认群：`INCIDENT_FEISHU_PARK_CHATS` 的廊坊，没有就用兜底常量。"""
  try:
    mapping = json.loads(settings.INCIDENT_FEISHU_PARK_CHATS or '{}')
  except ValueError:
    mapping = {}
  return (mapping.get(_PARK)
          or next(iter(mapping.values()), '')
          or _FALLBACK_CHAT_ID)


def _at_ts(value: str | None) -> int:
  """`--at 'YYYY-MM-DD HH:MM:SS'` → epoch；缺省用当前时刻（保证落进当前班次）。"""
  if not value:
    return int(datetime.now(_TZ).timestamp())
  return int(datetime.strptime(value, '%Y-%m-%d %H:%M:%S').replace(tzinfo=_TZ).timestamp())


def build_text(occurred_s: int, place: str = '廊坊园区A1数据中心 3F-301',
               overview: str = 'PDF验收-卡片速报（自动化测试）',
               value: float = float(_ALARM_VALUE)) -> str:
  """卡片正文：【发生时间】/告警值/地点可替换成**真实告警**，用于连匹配一起验。"""
  when = datetime.fromtimestamp(occurred_s, _TZ).strftime('%Y-%m-%d %H:%M:%S')
  return '\n'.join([
    '【廊坊园区A1数据中心 事件速报】',
    f'【发生时间】：{when}',
    f'【地点】：{place}',
    f'【事件概述】：{overview}',
    f'【告警值】：{value}',
    '【描述】：平台卡片解析自检，可忽略',
  ])


def build_card(text: str) -> str:
  """最小 markdown 卡（与 `service/feishu/reply.py:build_markdown_card` 同形）。"""
  card = {
    'config': {'wide_screen_mode': True},
    'elements': [{'tag': 'markdown', 'content': text}],
  }
  return json.dumps(card, ensure_ascii=False)


def _assert_parsed(text: str, occurred_s: int, value: float) -> dict:
  """离线断言：卡片正文经推/拉同一条链路解析后，各字段与预期一致。"""
  content = build_card(text)
  extracted = feishu.text_from_content('interactive', content)
  row = feishu.build_report_row(
    chat_id=_chat_id(), message_id='probe', create_s=occurred_s,
    sender='probe', sender_id_type='app_id', msg_type='interactive',
    text=extracted, raw_body=content,
  )
  checks = [
    ('卡片正文能抽出', extracted == text, extracted[:40]),
    ('kind=sos', row['kind'] == 'sos', row['kind']),
    ('标题', row['title'] == '廊坊园区A1数据中心 事件速报', row['title']),
    ('发生时间', row['event_time'] == occurred_s, row['event_time']),
    ('告警值', row['report_value'] == float(value), row['report_value']),
  ]
  failed = [(name, got) for name, ok, got in checks if not ok]
  for name, ok, got in checks:
    print(f'  {"OK " if ok else "!! "}{name}: {got}')
  if failed:
    raise SystemExit(f'卡片解析断言失败：{failed}')
  return row


async def _send(chat_id: str, text: str) -> str:
  from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

  client = feishu._client()
  request = (
    CreateMessageRequest.builder()
    .receive_id_type('chat_id')
    .request_body(
      CreateMessageRequestBody.builder()
      .receive_id(chat_id)
      .msg_type('interactive')
      .content(build_card(text))
      .build()
    )
    .build()
  )
  response = await asyncio.to_thread(client.im.v1.message.create, request)
  if response is None or not response.success():
    raise SystemExit('发送失败 code=%s msg=%s' % (
      getattr(response, 'code', '?'), getattr(response, 'msg', '')))
  message_id = getattr(getattr(response, 'data', None), 'message_id', '') or ''
  print(f'已发送 chat={chat_id} message_id={message_id}')
  return message_id


async def _recall(message_id: str) -> None:
  from lark_oapi.api.im.v1 import DeleteMessageRequest

  client = feishu._client()
  request = DeleteMessageRequest.builder().message_id(message_id).build()
  response = await asyncio.to_thread(client.im.v1.message.delete, request)
  if response is None or not response.success():
    raise SystemExit('撤回失败 code=%s msg=%s' % (
      getattr(response, 'code', '?'), getattr(response, 'msg', '')))
  print(f'已撤回 message_id={message_id}（拉取侧会把它当 recalled，物理删掉对应行）')


async def _check(chat_id: str, minutes: int) -> None:
  """拉最近 N 分钟的消息，只打印本脚本那张卡片的解析结果。"""
  now = int(datetime.now(_TZ).timestamp())
  rows, recalled = await feishu.fetch_messages(chat_id, now - minutes * 60, now + 120)
  hits = [r for r in rows if _MARK in (r.get('text') or '')]
  print(f'近 {minutes} 分钟：消息 {len(rows)} 条，命中测试卡片 {len(hits)} 条，'
        f'已撤回 {len(recalled)} 条')
  for row in hits:
    created = datetime.fromtimestamp(row['create_time'], _TZ)
    occurred = (datetime.fromtimestamp(row['event_time'], _TZ).strftime('%Y-%m-%d %H:%M:%S')
                if row.get('event_time') else '（空）')
    print(f'  message_id={row["message_id"]} create={created:%H:%M:%S} '
          f'kind={row["kind"]} title={row["title"]}')
    print(f'  event_time={occurred} report_value={row["report_value"]}')
  if not hits:
    print('  ! 没拉到测试卡片：确认 --send 已执行、chat_id 是否本群')


async def main() -> int:
  parser = argparse.ArgumentParser(description='卡片速报入库实测')
  parser.add_argument('--send', action='store_true', help='发送卡片到群')
  parser.add_argument('--check', action='store_true', help='拉取路径核对解析结果')
  parser.add_argument('--recall', metavar='MESSAGE_ID', help='撤回指定消息')
  parser.add_argument('--minutes', type=int, default=10, help='--check 的回看分钟数')
  parser.add_argument('--chat', default='', help='目标群 chat_id（缺省取园区映射）')
  parser.add_argument('--at', default='', help='【发生时间】YYYY-MM-DD HH:MM:SS，缺省当前时刻')
  parser.add_argument('--place', default='廊坊园区A1数据中心 3F-301', help='【地点】')
  parser.add_argument('--overview', default='PDF验收-卡片速报（自动化测试）',
                      help='【事件概述】；要连匹配一起验就填真实告警内容')
  parser.add_argument('--value', type=float, default=float(_ALARM_VALUE), help='【告警值】')
  args = parser.parse_args()

  chat_id = (args.chat or _chat_id()).strip()
  if args.recall:
    await _recall(args.recall.strip())
    return 0
  if args.check:
    await _check(chat_id, max(1, args.minutes))
    return 0

  occurred_s = _at_ts(args.at)
  text = build_text(occurred_s, args.place, args.overview, args.value)
  print(f'目标群 {chat_id}；发生时间 {datetime.fromtimestamp(occurred_s, _TZ)}')
  row = _assert_parsed(text, occurred_s, args.value)
  print(f'  机房码（用于匹配）: {row["room_code"] or "（无）"}')
  if not args.send:
    print('\n离线断言通过（未发送）。加 --send 才会真正推到群里。')
    return 0
  await _send(chat_id, text)
  print('提示：入库最多等一个补偿周期（30 分钟），或让平台查一次事件统计表触发回拉。')
  return 0


if __name__ == '__main__':
  sys.exit(asyncio.run(main()))
