"""合规检查自检（纯 assert，不连库、不依赖 pytest）。

跑法：`python -m service.scheduled_task.compliance.tests.test_compliance`

覆盖：班次窗口映射（cron 09:00/18:00 与手动触发的边界）、期号/标签、
行变换（级别/状态/结果解码、时长、重复组），列声明与行键一致，
空结果 xlsx 仍含全部 sheet 且表头完整。
"""

import tempfile
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from openpyxl import load_workbook

from service.scheduled_task.compliance import checks, xlsx

_TZ = ZoneInfo('Asia/Shanghai')
_passed = 0


def _ok(cond, label: str) -> None:
  global _passed
  assert cond, f'FAILED: {label}'
  _passed += 1


def _at(day: date, hour: int, minute: int) -> datetime:
  return datetime(day.year, day.month, day.day, hour, minute, tzinfo=_TZ)


# ==================== 班次窗口 ====================

def test_shift_window() -> None:
  today = date(2026, 10, 27)

  # cron 09:00 → 夜班（昨日 17:30 ~ 今日 08:30）
  win = checks.shift_window(_at(today, 9, 0))
  _ok(win['kind'] == 'night', '09:00 取夜班')
  _ok(win['key'] == '2026-10-27-n', '09:00 期号 = 结束日 + n')
  _ok(win['start'] == _at(date(2026, 10, 26), 17, 30), '夜班起点 10-26 17:30')
  _ok(win['end'] == _at(today, 8, 30), '夜班终点 10-27 08:30')
  _ok('2026-10-27 夜班' in win['label'] and '10-26 17:30' in win['label'],
      '夜班标签含日期与窗口')

  # cron 18:00 → 当日白班
  win = checks.shift_window(_at(today, 18, 0))
  _ok(win['kind'] == 'day' and win['key'] == '2026-10-27-d', '18:00 取当日白班')
  _ok(win['start'] == _at(today, 8, 30) and win['end'] == _at(today, 17, 30),
      '白班窗口 08:30~17:30')

  # 手动触发：未结束的班次不查
  _ok(checks.shift_window(_at(today, 10, 0))['key'] == '2026-10-27-n', '10:00 取夜班')
  _ok(checks.shift_window(_at(today, 16, 0))['key'] == '2026-10-27-n', '16:00 取夜班')
  _ok(checks.shift_window(_at(today, 2, 0))['key'] == '2026-10-26-d',
      '02:00 取昨日白班（夜班尚未结束）')
  _ok(checks.shift_window(_at(today, 8, 30))['key'] == '2026-10-27-n', '08:30 边界取夜班')
  _ok(checks.shift_window(_at(today, 17, 30))['key'] == '2026-10-27-d', '17:30 边界取白班')

  # 指定班次
  win = checks.shift_window_for('night', date(2026, 1, 1))
  _ok(win['key'] == '2026-01-01-n', '跨年夜班期号')
  _ok(win['start'] == _at(date(2025, 12, 31), 17, 30), '跨年夜班起点')

  _ok(checks.latest_end_day('night', _at(today, 2, 0)) == date(2026, 10, 26),
      'latest_end_day 夜班跨零点')
  _ok(checks.latest_end_day('day', _at(today, 18, 0)) == today,
      'latest_end_day 白班已结束')


# ==================== 格式化 ====================

def test_format() -> None:
  ts = int(_at(date(2026, 10, 27), 8, 30).timestamp())
  _ok(checks.fmt_ts(ts) == '2026-10-27 08:30:00', 'epoch → 北京时间字符串')
  _ok(checks.fmt_ts(0) == '' and checks.fmt_ts(None) == '', '0/空 → 空串')
  _ok(checks._minutes(100, 160) == 1.0, '分钟数')
  _ok(checks._minutes(0, 60) == '' and checks._minutes(160, 100) == '',
      '无效/倒挂 → 空串')

  # 大楼名称：园区短码 + 楼宇号（与值守台账 LF-A1 / PH-B1M1M2 的写法一致）
  _ok(checks._building_code('廊坊A', '廊坊园区A1数据中心') == 'LF-A1', '大楼名称 LF-A1')
  _ok(checks._building_code('平湖B', '平湖园区B1M1M2数据中心') == 'PH-B1M1M2',
      '大楼名称 PH-B1M1M2')
  _ok(checks._building_code('廊坊A', '') == '', '楼宇名缺失留空')
  _ok(checks._natural_key('LF-A2') < checks._natural_key('LF-A10'), '自然序 A2 < A10')


# ==================== 行变换（假数据，不打库）====================

def _with_query(rows: list[dict], fn, *args, buildings: dict | None = None):
  """替换 `_query` 与 `_buildings`（后者不打库，否则用例会真去连 PG）。"""
  original_query, original_buildings = checks._query, checks._buildings
  checks._query = lambda sql, args: rows
  checks._buildings = lambda: buildings or {}
  try:
    return fn(*args)
  finally:
    checks._query, checks._buildings = original_query, original_buildings


def test_row_transforms() -> None:
  now_s = int(_at(date(2026, 10, 27), 9, 0).timestamp())
  start_s = now_s - 3600
  end_s = now_s

  rows = _with_query([{
    'cluster': '廊坊A', 'order_no': 'wo-1', 'space_name': '廊坊园区A1数据中心',
    'device_name': '空调1', 'point_name': '回风温度', 'alarm_level': 4,
    'content': '温度过高', 'alarm_time': now_s - 1200,
    'response_time': now_s - 600, 'responder': '张三', 'handler': '李四',
    'alarm_status': '1',
  }], checks.response_timeout, start_s, end_s, now_s)
  row = rows[0]
  _ok(row['级别'] == '紧急' and row['工单号'] == 'wo-1', '告警级别解码/工单号')
  _ok(row['园区'] == '廊坊', 'cluster → 园区名')
  _ok(row['响应时长(分钟)'] == 10.0, '响应时长 = 响应 − 告警')
  _ok(row['告警时间'] == checks.fmt_ts(now_s - 1200), '告警时间渲染')
  _ok(row['响应时间'] == checks.fmt_ts(now_s - 600), '响应时间渲染')
  _ok(row['当前告警状态'] == '触发', '告警状态解码')

  rows = _with_query([{
    'cluster': '平湖B', 'order_no': 'wo-1', 'space_name': '平湖园区', 'device_name': 'UPS',
    'point_name': '输出电压', 'alarm_level': 3, 'content': '电压异常',
    'alarm_time': now_s - 1800, 'response_time': now_s - 900, 'start_time': now_s - 800,
    'responder': '张三', 'handler': '', 'alarm_status': '1',
  }], checks.unclosed_order, start_s, end_s, now_s)
  row = rows[0]
  _ok(row['开始处理时间'] == checks.fmt_ts(now_s - 800), '开始处理时间')
  _ok(row['响应时长(分钟)'] == 15.0, '响应时长')
  _ok(row['未关单时长(分钟)'] == 15.0, '未关单时长按响应时刻起算')
  _ok(row['当前告警状态'] == '触发', '告警状态解码')

  rows = _with_query([{
    'cluster': '佛山C', 'order_no': 'wo-2', 'space_name': '佛山园区', 'device_name': 'PDU',
    'point_name': '电流', 'alarm_level': 3, 'content': '过流',
    'alarm_time': now_s - 7200, 'start_time': now_s - 3000, 'end_time': now_s - 1800,
    'handler': '李四', 'process_result': 2, 'alarm_status': '2',
  }], checks.premature_close, start_s, end_s, now_s)
  row = rows[0]
  _ok(row['处理结果'] == '已恢复正常', '处理结果解码')
  _ok(row['处理时长(分钟)'] == 20.0, '处理时长 = end - start')
  _ok(row['告警已持续(分钟)'] == 120.0, '告警已持续从发生算起')
  _ok(row['当前告警状态'] == '刷新', '刷新态也算未解除')

  rows = _with_query([{
    'day': date(2026, 10, 27), 'cluster': '惠州D', 'space_guid': 'sp-1',
    'space_name': '惠州园区D数据中心M1一层101机房', 'device_name': '空调2',
    'point_name': '送风温度', 'max_level': 3, 'content': '温度过高',
    'n': 4, 'first_s': now_s - 3000, 'last_s': now_s - 600,
    'last_alarm_status': '2', 'process': '现场已调整空调参数',
    'process_result': 2, 'order_status': 2, 'order_no': 'wo-9',
  }], checks.repeat_alarm, start_s, end_s, now_s,
      buildings={('惠州D', 'sp-1'): '惠州园区D数据中心'})
  row = rows[0]
  _ok(row['大楼名称'] == 'HZ-D', '大楼名称 = 园区短码 + 楼宇号')
  _ok(row['告警时间'] == checks.fmt_ts(now_s - 600), '告警时间取末次')
  _ok(row['首次时间'] == checks.fmt_ts(now_s - 3000), '首次时间')
  _ok(row['次数'] == 4 and row['等级'] == '重要', '重复告警次数/等级')
  _ok(row['处理过程'] == '现场已调整空调参数', '处理过程取关联工单')
  _ok(row['处理结果'] == '已恢复正常', '处理结果按 process_result 解码')
  _ok(row['问题是否解决'] == '' and row['解决办法'] == '' and row['填报人'] == '',
      '人工填报列留空')
  _ok(row['跨度(分钟)'] == 40.0, '重复告警跨度')

  # 无工单 → 按告警状态降级（与事件统计表 `rules.event_progress` 同源）
  rows = _with_query([{
    'day': date(2026, 10, 27), 'cluster': '廊坊A', 'space_guid': 'sp-2',
    'space_name': '', 'device_name': '', 'point_name': '', 'max_level': 4,
    'content': '', 'n': 3, 'first_s': now_s - 600, 'last_s': now_s,
    'last_alarm_status': '3', 'process': None, 'process_result': None,
    'order_status': None, 'order_no': None,
  }], checks.repeat_alarm, start_s, end_s, now_s)
  _ok(rows[0]['处理结果'] == '已解除', '无工单 + 已恢复 → 已解除')
  _ok(rows[0]['大楼名称'] == '', '楼宇名解析不到时留空')


def test_columns_match_rows() -> None:
  """列声明与行键必须一致（加列漏改这里会炸）。"""
  now_s = int(_at(date(2026, 10, 27), 9, 0).timestamp())
  samples = {
    'response_timeout': [{
      'cluster': '廊坊A', 'order_no': '', 'space_name': '', 'device_name': '',
      'point_name': '', 'alarm_level': 3, 'content': '', 'alarm_time': now_s - 60,
      'response_time': now_s, 'responder': '', 'handler': '', 'alarm_status': '3',
    }],
    'unclosed_order': [{
      'cluster': '廊坊A', 'order_no': '', 'space_name': '', 'device_name': '',
      'point_name': '', 'alarm_level': 3, 'content': '', 'alarm_time': now_s - 60,
      'response_time': now_s, 'start_time': now_s, 'responder': '', 'handler': '',
      'alarm_status': '3',
    }],
    'premature_close': [{
      'cluster': '廊坊A', 'order_no': '', 'space_name': '', 'device_name': '',
      'point_name': '', 'alarm_level': 3, 'content': '', 'alarm_time': now_s - 60,
      'start_time': now_s - 60, 'end_time': now_s, 'handler': '',
      'process_result': 1, 'alarm_status': '1',
    }],
    'repeat_alarm': [{
      'day': date(2026, 10, 27), 'cluster': '廊坊A', 'space_guid': 'sp-1',
      'space_name': '', 'device_name': '', 'point_name': '', 'max_level': 3,
      'content': '', 'n': 3, 'first_s': now_s - 60, 'last_s': now_s,
      'last_alarm_status': '3', 'process': None, 'process_result': None,
      'order_status': None, 'order_no': None,
    }],
  }
  for check in checks.CHECKS:
    rows = _with_query(samples[check['key']], checks._RUNNERS[check['key']],
                       now_s - 3600, now_s, now_s)
    columns = checks.COLUMNS[check['key']]
    _ok(tuple(rows[0].keys()) == columns, f"{check['key']} 行键顺序与列声明一致")
  _ok(set(checks._RUNNERS) == {c['key'] for c in checks.CHECKS}, '运行器与注册表一一对应')


# ==================== 汇总 / 总览矩阵 ====================

def test_summary_matrix() -> None:
  rows_by_key = {
    'response_timeout': [{'园区': '廊坊'}, {'园区': '平湖'}],
    'unclosed_order': [{'园区': '廊坊'}],
    'premature_close': [],
    'repeat_alarm': [{'园区': '廊坊'}],
  }
  summary = checks.summary(rows_by_key)
  _ok([s['rows'] for s in summary] == [2, 1, 0, 1], '汇总命中条数')
  _ok([s['name'] for s in summary] == [c['name'] for c in checks.CHECKS],
      '汇总顺序与注册表一致')

  matrix = checks.matrix(rows_by_key)
  langfang = next(r for r in matrix if r['园区'] == '廊坊')
  _ok(langfang['总计'] == 3, '廊坊合计')
  total = matrix[-1]
  _ok(total['园区'] == '总计' and total['总计'] == 4, '矩阵总计行')

  _ok(checks.matrix({}) == [], '全 0 时矩阵为空（不造假行）')


# ==================== xlsx ====================

def test_xlsx_empty_and_full() -> None:
  empty = {'rows': {c['key']: [] for c in checks.CHECKS}, 'summary': [], 'matrix': []}
  empty['summary'] = checks.summary(empty['rows'])
  with tempfile.TemporaryDirectory() as tmp:
    path = Path(tmp) / 'compliance.xlsx'
    xlsx.write_workbook(path, empty, title='运营监控中心告警不合规检查 · 测试',
                        period='测试期')
    wb = load_workbook(path)
    expected = [xlsx.OVERVIEW_SHEET, *[c['sheet'] for c in checks.CHECKS]]
    _ok(wb.sheetnames == expected, '0 行也出全部 sheet')
    for check in checks.CHECKS:
      _ok(wb[check['sheet']].cell(row=1, column=1).value
          == f"{check['title']} · 测试期", f"{check['sheet']} 标题行")
      header = [c.value for c in wb[check['sheet']][2]]
      _ok(tuple(header) == checks.COLUMNS[check['key']],
          f"{check['sheet']} 表头完整")
      _ok(wb[check['sheet']].max_row == 2, f"{check['sheet']} 0 行只有标题+表头")
    overview = wb[xlsx.OVERVIEW_SHEET]
    first_col = [overview.cell(row=r, column=1).value for r in range(1, 8)]
    _ok('检查项' in first_col, '总览含检查项表头')
    wb.close()

    full = {'rows': dict(empty['rows']), 'summary': [], 'matrix': []}
    full['rows']['repeat_alarm'] = [{
      '序号': 1, '大楼名称': 'LF-A1', '告警时间': '2026-10-27 02:00:00',
      '等级': '重要', '次数': 3, '地址名称': '廊坊园区A1数据中心M1模组一层101机房',
      '设备名称': '空调', '监控项名称': '回风温度', '告警内容': '温度过高',
      '处理过程': '现场已调整', '处理结果': '已恢复正常', '问题是否解决': '',
      '解决办法': '', '填报人': '', '首次时间': '2026-10-27 01:00:00',
      '跨度(分钟)': 60.0,
    }]
    full['summary'] = checks.summary(full['rows'])
    full['matrix'] = checks.matrix(full['rows'])
    path = Path(tmp) / 'full.xlsx'
    xlsx.write_workbook(path, full, title='标题')
    wb = load_workbook(path)
    ws = wb['重复告警']
    header = [c.value for c in ws[2]]
    _ok(ws.cell(row=1, column=1).value == '数据中心重复告警统计', '无 period 时标题只写检查项')
    _ok(ws.max_row == 3 and ws.cell(row=3, column=header.index('次数') + 1).value == 3,
        '数据行落盘')
    _ok(header[:14] == ['序号', '大楼名称', '告警时间', '等级', '次数', '地址名称',
                        '设备名称', '监控项名称', '告警内容', '处理过程', '处理结果',
                        '问题是否解决', '解决办法', '填报人'],
        '重复告警前 14 列与值守台账逐字一致')
    _ok(wb[xlsx.OVERVIEW_SHEET].cell(row=1, column=1).value == '标题', '总览标题行')
    wb.close()


def main() -> None:
  test_shift_window()
  test_format()
  test_row_transforms()
  test_columns_match_rows()
  test_summary_matrix()
  test_xlsx_empty_and_full()
  print(f'合规检查自检通过：{_passed} 项')


if __name__ == '__main__':
  main()
