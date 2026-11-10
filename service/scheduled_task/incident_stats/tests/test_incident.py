"""事件统计表口径自检（**不依赖测试框架**，纯 assert + 非零退出码）。

覆盖：事件类型规则（示例表 40 种标签）、违规文案（示例表反证的边界）、时长文案、
通报配对（速报/首报/无告警）、20 列结构与双 Sheet 输出。
"""

import json
import tempfile
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from openpyxl import load_workbook

from service.scheduled_task.incident_stats import (
  arbitrate, build, detail, extract, feishu, rules, source, sync, xlsx,
)

_TZ = ZoneInfo('Asia/Shanghai')
_T0 = int(datetime(2026, 10, 12, 10, 0, 0, tzinfo=_TZ).timestamp())

# 华为 eWatch 告警推送原文（现场 2026-10-20 实测样式）：字段不是【】标签
_EWATCH = (
  '【华为】<eWatch><告警产生>\n'
  '对象:rz6241-4b-01-03 (A6M24012-3FW)\n'
  '机房:rz6241 (润泽信息港 A6-M2-4F-401 风冷机房)\n'
  '时间：2026-10-20 16:40:44 GMT+08:00\n'
  '监控项：BranchPower\n'
  '告警值：3366.0\n'
  '描述：机柜支路功率超过安全值 (安全值 = 电流*电压*0.95=3344.00W, 电流 = 16.00A, '
  '电压 = 220.00V)(支路分区：A2/B2; 支路分区设备 SN 列表:[2102313HSJ10M2000019])\n'
  '级别：警告\n'
  '来源：廊坊 CDC（润泽信息港）,@润泽 A6 数据中心识别到华为，告警产生关键字时可确认'
)

# 同一推送的**单行**形态（2026-10-29 现场实测）：字段全挤在一行，行首锚定的正则取不到，
# 曾导致「发生时间」退回消息 create_time、与「速报时间」一模一样（差 67 分钟）。
_EWATCH_SINGLE = (
  '【华为】<eWatch><告警产生> 对象:rz6241-4b-01-03 (A6M24012-3FW) '
  '机房:rz6241 (润泽信息港 A6-M2-4F-401 风冷机房) 时间：2026-10-29 16:07:00 GMT+08:00 '
  '监控项：BranchPower 告警值：37.27 '
  '描述：机柜支路功率超过安全值，预计 2026年10月30日 完成处置 级别：警告'
)

# 卡片式事件通报（2026-10-29 现场样式）：标题多一个「事件」，冒号写在【】内，
# 后面「进展同步N」原地追加。字段不是「数据中心速报/首报」，靠关键字「通报」认首报。
_CARD = (
  '【RZ-CQ-A1数据中心事件通报】 \n'
  '【事件概述：】 : 3F-B配电室-A1M1-3F-B配电室-UPS-B3U5-UPS正常模式-告警\n'
  '【发生时间：】 : 2026年10月29日03时25分04秒\n'
  '【事件来源：】 : DCIM\n'
  '【涉及客户：】 : [T客户]\n'
  '【影响范围：】 : 3F-B配电室\n'
  '【处置措施：】 : 人员以前往现场查看\n'
  '【通知情况】 : 已通知值班二线工程师\n'
  '－－－－－－－－－－－－－－\n'
  '进展同步1\n'
  '【通报时间】 : 2026年10月29日03时27分31秒\n'
  '【进展描述】 : 1.人员已前往现场应急\n'
  '－－－－－－－－－－－－－－\n'
  '进展同步2\n'
  '【通报时间】 : 2026年10月29日03时28分12秒\n'
  '【进展描述】 : 1.现场人员确认为UPS逆变器温度高告警'
)


def test_event_types():
  cases = {
    'M2-4F-电池室B-B6Z2-3-T1组-第05节内阻-内阻低报警': '电池内阻低',
    '电池内阻偏高告警': '电池内阻高',
    'B1-电池组均差值过高': '电池均差值过高',
    '3F电池整组电压过高报警': '电池组电压过高',
    '电池整组电压过低': '电池组电压过低',
    '电池温度过低报警': '电池温度低',
    '电池温度过高': '电池温度高',
    '电池内阻异常': '电池内阻异常',
    '空调配电柜通讯中断': '空调配电柜设备通讯中断',
    '电池开关柜通讯中断': '电池开关柜通讯中断',
    '低压柜A通讯中断': '低压柜通讯中断',
    '温湿度传感器通讯中断': '温湿度通讯中断',
    '空调1#通讯中断': '空调通讯中断',
    '电池巡检仪通讯中断': '电池通讯中断',
    '嵌入式通讯中断': '嵌入式通讯中断',
    'UPS通讯中断': '设备通讯中断',
    '空调1进水温度过高': '空调进水温度过高',
    '冷塔1水槽温度高': '冷塔水槽温度高',
    '冷塔2供水压力低': '冷塔供水压力低',
    'A3-5F-机房温度过高': '机房单点高温',
    '市电A相电压过低': '市电波动',
    'UPS1主进开关断开': 'UPS主进断开',
    '列头柜3开关状态异常': '机柜单路掉电',
    'UPS旁路频率越限': 'UPS旁路频率越限',
    '二次泵B路断开': '二次泵AB路断开',
    '列头柜1单线电流归零': '列头柜单线电流归零',
    '列头柜2电流归零': '列头柜电流归零',
    '列头柜总告警': '列头柜总告警',
    '冷机报警代码 12': '冷机报警代码12',
    '恒湿机A水位低': '恒湿机水位低',
    '恒湿机过载': '恒湿机故障',
    '除湿机漏水报警': '除湿机漏水',
    '漏水绳断开': '漏水绳断开',
    '水泵房漏水': '水泵房漏水',
    '空调间漏水': '空调间漏水',
    'B1-3F漏水报警': '漏水报警',
    '油箱1液位高': '油箱液位高',
  }
  for name, want in cases.items():
    got = rules.event_type(name)
    assert got == want, f'事件类型 {name!r}: {got!r} != {want!r}'
  # 兜底：取最后一段、去掉尾部「报警/告警」
  assert rules.event_type('UPS整流器故障报警') == 'UPS整流器故障'
  assert rules.event_type('') == ''


def test_violation():
  # 示例表反证：速报 121/124 判、118/120 不判；首报 304/312 判、297/298 不判
  assert rules.violation(_T0, _T0 + 121, _T0 + 300) == '速报超时1秒'
  assert rules.violation(_T0, _T0 + 124, _T0 + 300) == '速报超时4秒'
  assert rules.violation(_T0, _T0 + 118, _T0 + 297) == '无'
  assert rules.violation(_T0, _T0 + 120, _T0 + 298) == '无'
  assert rules.violation(_T0, _T0 + 100, _T0 + 304) == '首报超时4秒'
  assert rules.violation(_T0, _T0 + 100, _T0 + 312) == '首报超时12秒'
  # 超时文案：分/秒换算
  assert rules.violation(_T0, _T0 + 304, _T0 + 900) == '速报超时3分4秒、首报超时10分钟'
  # 缺通报
  assert rules.violation(_T0, None, _T0 + 100) == '缺速报'
  assert rules.violation(_T0, _T0 + 100, None) == '缺首报'
  assert rules.violation(_T0, None, None) == '缺速报、缺首报'
  # 发生时间不明 → 不猜
  assert rules.violation(None, _T0, _T0) == ''


def test_extract_event_time():
  want = int(datetime(2026, 10, 13, 14, 30, 0, tzinfo=_TZ).timestamp())
  assert feishu.extract_event_time('【发生时间】2026年10月13日14时30分00秒') == want
  assert feishu.extract_event_time('【发生时间】2026年10月13日,14:30:00') == want
  assert feishu.extract_event_time('【发生时间】2026 年 10 月 13 日 14 时 30 分 00 秒') == want
  assert feishu.extract_event_time('【发生时间】2026年10月13日14:30') == want
  assert feishu.extract_event_time('没有时间') is None
  # 无量纲非法日期不抛
  assert feishu.extract_event_time('2026年13月40日10时00分00秒') is None


def test_classify():
  assert feishu.classify('【RZ-FS-A3数据中心速报】') == 'sos'
  assert feishu.classify('【RZ-FS-A3数据中心首报】') == 'first'
  # 现场第二条报文写「通报」（如 `【RZ-LF-A12数据中心通报】`），与首报同义
  assert feishu.classify('【RZ-LF-A12数据中心通报】') == 'first'
  assert feishu.classify('数据中心 【速报】 事件') == 'sos'
  assert feishu.classify('数据中心[首报]') == 'first'
  assert feishu.classify('其他消息') == 'other'
  # 华为 eWatch 推送 = 速报的另一种格式；**只有「告警产生」算速报**
  assert feishu.classify(_EWATCH) == 'sos'
  assert feishu.classify(_EWATCH.replace('<告警产生>', '<告警恢复>')) == 'other'
  # 首报里引用 eWatch 原文时仍算首报（先认「数据中心首报」）
  assert feishu.classify('【RZ-LF-A12数据中心首报】\n' + _EWATCH) == 'first'

  # 用户口径：关键字字面匹配，不要求「数据中心」前缀
  assert feishu.classify(_CARD) == 'first'                       # 卡片「事件通报」= 首报
  assert feishu.classify('【RZ-CQ-A1数据中心事件速报】') == 'sos'
  assert feishu.classify('速报：M1-204 机房漏水') == 'sos'
  # 速报优先于通报 —— 速报正文常写「已通报二线」，先判通报会把速报算成首报
  assert feishu.classify('【RZ-FS-A3数据中心速报】\n已通报二线工程师') == 'sos'
  assert feishu.classify('今天进展怎么样') == 'other'


def test_report_field():
  # 报文布局按《3.2.1 事件管理流程》表 2-2 的实际字段名
  text = ('【事件概述】AXX 数据中心 2 层 XX 变配电室 XX UPS 逆变器故障\n'
          '【发生时间】2026 年 10 月 13 日,14:30:00\n'
          '【影响范围】AXX 数据中心,XX 机房 XX 路\n'
          '【事件类型】IT 负载 UPS 故障')
  assert build._report_field(text, ('事件概述', '事件名称')).startswith('AXX 数据中心 2 层')
  assert build._report_field(text, ('地点', '影响范围')) == 'AXX 数据中心,XX 机房 XX 路'
  assert build._report_field(text, ('事件类型',)) == 'IT 负载 UPS 故障'
  assert build._report_field('', ('事件概述',)) == ''
  # 卡片把冒号写在括号内、标签后再跟一个冒号：两种写法都要取到干净的「值」
  for text in ('【事件概述：】 : 市电B路电压过低', '【事件概述】:市电B路电压过低'):
    assert build._report_field(text, ('事件概述',)) == '市电B路电压过低', text


def test_duration_text():
  assert rules.duration_text(_T0, _T0 + 30) == '不足1分钟'
  assert rules.duration_text(_T0, _T0 + 600) == '10分钟'
  assert rules.duration_text(_T0, _T0 + 3660) == '1小时1分钟'
  assert rules.duration_text(_T0, _T0 + 7200) == '2小时'
  assert rules.duration_text(_T0, _T0 - 60) == ''      # 倒挂
  assert rules.duration_text(None, _T0) == ''


def test_place_and_park():
  assert rules.park_of('廊坊A') == ('廊坊', 'LF')
  assert rules.park_of('未知集群') == ('未知集群', '未知集群')
  assert rules.place('廊坊A', 'A12数据中心') == '廊坊园区A12数据中心'
  assert rules.place('廊坊A', 'A12') == '廊坊园区A12数据中心'
  assert rules.place('廊坊A', '') == '廊坊'
  assert rules.place('平湖A', '廊坊园区A12数据中心') == '平湖园区廊坊园区A12数据中心'


def _report(kind, ts, cluster='廊坊', event_log_id=None, text='', event_time=None):
  return {
    'kind': kind, 'create_time': ts, 'cluster': cluster,
    'event_log_id': event_log_id, 'text': text,
    'event_time': event_time if event_time is not None else ts,
  }


def test_group_reports():
  # 同一告警的速报 + 两条通报（补发）→ 仍是一组
  groups = build._group_reports([
    _report('sos', _T0 + 60, event_log_id='E1'),
    _report('first', _T0 + 120, event_log_id='E1'),
    _report('first', _T0 + 600, event_log_id='E1'),
  ])
  assert len(groups) == 1 and len(groups[0]['reports']) == 3

  # 同一个秒但**不同告警**（E1/E2）→ 两个组，绝不按时间合并
  groups = build._group_reports([
    _report('first', _T0 + 120, event_log_id='E1'),
    _report('first', _T0 + 120, event_log_id='E2'),
  ])
  assert len(groups) == 2

  # 无告警：速报就近吸附到同园区首报（±900s）
  groups = build._group_reports([
    _report('first', _T0 + 300),
    _report('sos', _T0 + 240),
  ])
  assert len(groups) == 1 and len(groups[0]['reports']) == 2

  # 超出 900s → 各成一行
  groups = build._group_reports([
    _report('first', _T0 + 300),
    _report('sos', _T0 - 700),
  ])
  assert len(groups) == 2

  # 只有速报 → 仍出行
  groups = build._group_reports([_report('sos', _T0)])
  assert len(groups) == 1


def test_repeated_reports_latest_wins():
  """同一告警补发多条通报：时间列取最早，正文/名称取最新，进展同步到最后一条。"""
  first_text = ('【发生时间】2026-10-12 10:00:00\n'
                '【地点】廊坊园区A12数据中心\n'
                '【事件概述】电池内阻低报警\n'
                '【事件类型】电池内阻低')
  second_text = ('【发生时间】2026-10-12 10:00:00\n'
                 '【地点】廊坊园区A12数据中心\n'
                 '【事件概述】电池内阻低报警（复核）\n'
                 '【事件类型】电池内阻低\n'
                 '进展同步1\n【进展描述】已更换单体电池，电压回升')
  reports = [
    _report('sos', _T0 + 60, event_log_id='E1', text='【事件概述】速报'),
    _report('first', _T0 + 120, event_log_id='E1', text=first_text),
    _report('first', _T0 + 600, event_log_id='E1', text=second_text,
            event_time=_T0 - 60),
  ]
  row = build._compose({'reports': reports}, None, {}, 1)
  assert row['速报时间'] == rules.fmt_event_time(_T0 + 60)
  assert row['首报时间'] == rules.fmt_event_time(_T0 + 120)      # 最早那条
  assert row['事件名称'] == '电池内阻低报警（复核）'               # 后发覆盖
  assert row['事件进展'] == '已更换单体电池，电压回升'
  assert row['发生时间'] == rules.fmt_event_time(_T0 - 60)       # 最新非空 event_time

  # 后发通报**只带进展**（不重复【事件概述】）→ 名称不被冲空，进展照旧取最新
  late = _report('first', _T0 + 900, event_log_id='E1',
                 text='进展同步2\n【进展描述】已恢复，观察 30 分钟')
  late['event_time'] = None
  row = build._compose({'reports': [reports[1], late]}, None, {}, 1)
  assert row['事件名称'] == '电池内阻低报警'
  assert row['事件类型'] == '电池内阻低'
  assert row['首报时间'] == rules.fmt_event_time(_T0 + 120)
  assert row['事件进展'] == '已恢复，观察 30 分钟'


def test_compose_without_alarm():
  text = ('【发生时间】2026年10月12日10时00分00秒\n'
          '【地点】廊坊园区A12数据中心\n'
          '【事件概述】电池内阻低报警\n'
          '【事件类型】电池内阻低')
  row = build._compose({'reports': [_report('first', _T0, text=text)]}, None, {}, 1)
  assert row is not None
  assert row['事件名称'] == '电池内阻低报警'
  assert row['事件类型'] == '电池内阻低'
  assert row['地点'] == '廊坊园区A12数据中心'
  assert row['事件进展'] == ''            # 未匹配告警 → 留空
  assert row['处理结果'] == ''
  assert row['完成时间'] == ''
  assert row['过程有无违规'] == '缺速报'
  # 有名称就不重复放原文
  assert row['速报内容'] == '' and row['首报内容'] == ''
  assert len(row) == len(build.COLUMNS) == 22


def _alarm(**kw):
  base = {
    'cluster': '廊坊A', 'space_guid': 'S1', 'alarm_time': _T0, 'alarm_status': '3',
    'device_name': '电池', 'point_name': '内阻', 'content': 'T1组-内阻低报警',
    'order_no': None, 'order_status': None, 'process': None, 'process_result': None,
    'start_time': None, 'end_time': None,
  }
  base.update(kw)
  return base


def test_compose_with_alarm_and_order():
  group = {'reports': [_report('sos', _T0 + 60, event_log_id='E1'),
                       _report('first', _T0 + 120, event_log_id='E1')]}
  buildings = {('廊坊A', 'S1'): 'A12数据中心'}

  # 无工单、告警已恢复 → 已解除
  row = build._compose(group, _alarm(), buildings, 1)
  assert row['发生时间'] == rules.fmt_event_time(_T0)
  assert row['速报时间'] == rules.fmt_event_time(_T0 + 60)
  assert row['首报时间'] == rules.fmt_event_time(_T0 + 120)
  assert row['地点'] == '廊坊园区A12数据中心'
  # 事件名称 = 地点 + 设备 + 测点 + 告警名称；事件类型 = 告警名称
  assert row['事件名称'] == '廊坊园区A12数据中心 - 电池 - 内阻 - T1组-内阻低报警'
  assert row['事件类型'] == 'T1组-内阻低报警'
  assert row['事件进展'] == '已解除'
  assert row['处理结果'] == '已解除'
  assert row['过程有无违规'] == '无'
  assert row['速报内容'] == '' and row['首报内容'] == ''

  # 有工单：原因/进展/完成时间/时长
  alarm = _alarm(order_no='WO1', order_status='2', process='电池老化',
                 process_result='2', start_time=_T0, end_time=_T0 + 3660)
  row = build._compose(group, alarm, buildings, 1)
  assert row['事件原因'] == '电池老化'
  assert row['事件进展'] == '已恢复正常'
  assert row['处理结果'] == '已恢复正常'
  assert row['完成时间'] == rules.fmt_event_time(_T0 + 3660)
  assert row['处理时长'] == '1小时1分钟'

  # 有工单未出结果 → 处理中
  row = build._compose(group, _alarm(order_no='WO2', process_result=None), buildings, 1)
  assert row['事件进展'] == '处理中'

  # 无工单、告警未恢复 → 未处理
  row = build._compose(group, _alarm(alarm_status='1'), buildings, 1)
  assert row['事件进展'] == '未处理'


def test_place_uses_full_space_name():
  """地点直接取告警空间全名（`dcim_spaces.name`），取不到才降级楼宇/园区。"""
  group = {'reports': [_report('sos', _T0 + 60, event_log_id='E1'),
                       _report('first', _T0 + 120, event_log_id='E1')]}
  buildings = {('廊坊A', 'S1'): 'A12数据中心'}
  full = '廊坊园区A12数据中心M1模组一层101机房'
  spaces = {('廊坊A', 'S1'): full}

  row = build._compose(group, _alarm(), buildings, 1, spaces)
  assert row['地点'] == full
  assert row['事件名称'] == f'{full} - 电池 - 内阻 - T1组-内阻低报警'

  # 空间名缺失 → 楼宇（既有行为）；空间名空白串同样降级
  assert build._compose(group, _alarm(), buildings, 1, {})['地点'] == '廊坊园区A12数据中心'
  assert build._compose(group, _alarm(), buildings, 1, {('廊坊A', 'S1'): ' '})['地点'] == \
      '廊坊园区A12数据中心'
  # 楼宇也解析不到 → 裸园区名
  assert build._compose(group, _alarm(), {}, 1, {})['地点'] == '廊坊'


def test_occurred_at_prefers_trigger_time():
  """发生时间/统计期归属取**触发态**时间；当前态 alarm_time 只作老告警兜底。"""
  group = {'reports': [_report('first', _T0 + 120, event_log_id='E1')]}
  alarm = _alarm(alarm_time=_T0 + 3600, trigger_time=str(_T0))
  row = build._compose(group, alarm, {}, 1)
  assert row['发生时间'] == rules.fmt_event_time(_T0)
  assert row['日期'] == datetime.fromtimestamp(_T0, _TZ).date()

  # 没有历史（老告警）→ 回退当前态
  row = build._compose(group, _alarm(alarm_time=_T0 + 3600), {}, 1)
  assert row['发生时间'] == rules.fmt_event_time(_T0 + 3600)


def test_summary_counts():
  rows = [{'地点': '廊坊园区A12数据中心', '事件等级': ''},
          {'地点': '廊坊园区A7数据中心', '事件等级': ''},
          {'地点': '平湖园区A1数据中心', '事件等级': ''}]
  out = build.summary_counts(rows)
  assert [r['园区'] for r in out] == ['平湖', '廊坊', '总计']
  # 事件等级留空 → 各档均为 0（汇总表结构先立住，等级接入后自动有数）
  assert all(r['I0'] == 0 for r in out)
  assert out[-1]['总计'] == 0


def test_xlsx_layout():
  rows = [build._compose({'reports': [_report('sos', _T0)]}, None, {}, 1)]
  summary = build.summary_counts(rows)
  with tempfile.TemporaryDirectory() as tmp:
    path = Path(tmp) / 'incident.xlsx'
    xlsx.write_workbook(path, rows, summary, _T0)
    wb = load_workbook(path)
    assert wb.sheetnames == [xlsx.sheet_title(_T0), '事件汇总']
    ws = wb[wb.sheetnames[0]]
    assert [c.value for c in ws[1]] == list(build.COLUMNS)
    assert ws.max_row == 2
    # 0 行也出完整表头
    path0 = Path(tmp) / 'empty.xlsx'
    xlsx.write_workbook(path0, [], [], _T0)
    assert load_workbook(path0)[wb.sheetnames[0]].max_row == 1


def test_xlsx_row_height_follows_content():
  """长文本行按内容撑高（封顶 8 行），短行保持紧凑；不靠手动拖行高。"""
  assert xlsx._text_width('中文abc') == 7
  assert xlsx._wrapped_lines('短文本', 20) == 1
  assert xlsx._wrapped_lines('a\nb', 20) == 2
  assert xlsx._wrapped_lines('x' * 200, 20) == 11       # 容量 = 20 - 1
  assert xlsx._row_height([('速报内容', 'x' * 1000)]) == 112.0   # 8 行封顶
  assert xlsx._row_height([('序号', 1), ('日期', date(2026, 10, 12))]) == 17.5

  short = build._compose({'reports': [_report('sos', _T0)]}, None, {}, 1)
  tall = dict(short, **{'速报内容': '速报正文' * 400})
  with tempfile.TemporaryDirectory() as tmp:
    path = Path(tmp) / 'incident.xlsx'
    xlsx.write_workbook(path, [short, tall], [], _T0)
    ws = load_workbook(path)[xlsx.sheet_title(_T0)]
    short_h = ws.row_dimensions[2].height
    tall_h = ws.row_dimensions[3].height
    assert short_h is not None and tall_h is not None
    assert short_h < tall_h <= 112.0
    # 列宽仍按 _WIDTHS，长文本列放宽过
    assert ws.column_dimensions['U'].width == 56      # 速报内容


def _ts(*args):
  return int(datetime(*args, tzinfo=_TZ).timestamp())


_MESSAGE = (
  '【RZ-FS-A3 数据中心速报】\n'
  '【发生时间】：2026 年 10 月 5 日 01:45:04\n'
  '【事件概述】:M1-204 机房 - 市电 B 路电表 CA 线电压过低\n'
  '【告警值】:9.25（已编辑）'
)


def test_parse_event_time_variants():
  assert feishu.parse_event_time(_MESSAGE) == _ts(2026, 10, 5, 1, 45, 4)
  # 字段顺序打乱不影响
  assert feishu.parse_event_time(
    '【事件概述】X\n【告警值】:1\n【发生时间】：2026年10月5日01:45:04') == \
    _ts(2026, 10, 5, 1, 45, 4)
  # 同一条里先出现【恢复时间】→ 必须锚定【发生时间】
  assert feishu.parse_event_time(
    '【恢复时间】2026年10月5日03:20:00\n【发生时间】2026年10月5日01:45:04') == \
    _ts(2026, 10, 5, 1, 45, 4)
  # 横杠 / 斜杠 / ISO
  assert feishu.parse_event_time('【发生时间】2026-10-05 01:45:04') == _ts(2026, 10, 5, 1, 45, 4)
  assert feishu.parse_event_time('【发生时间】2026/10/05 01:45') == _ts(2026, 10, 5, 1, 45, 0)
  assert feishu.parse_event_time('【发生时间】2026-10-05T01:45:04') == _ts(2026, 10, 5, 1, 45, 4)
  # 下午 → +12h；上午不补
  assert feishu.parse_event_time('【发生时间】2026年10月5日 下午 1:45:04') == \
    _ts(2026, 10, 5, 13, 45, 4)
  assert feishu.parse_event_time('【发生时间】2026年10月5日 上午 1:45:04') == \
    _ts(2026, 10, 5, 1, 45, 4)
  # 只有日期 → None（不用 00:00 硬凑）；非法日期不抛
  assert feishu.parse_event_time('【发生时间】2026年10月5日') is None
  assert feishu.parse_event_time('【发生时间】2026年13月40日10时00分00秒') is None
  assert feishu.parse_event_time('没有时间') is None


def test_parse_event_time_year_inference():
  ref = _ts(2026, 1, 2, 9, 0, 0)
  assert feishu.parse_event_time('【发生时间】1月1日 08:00', ref) == _ts(2026, 1, 1, 8, 0, 0)
  # 比消息晚 180 天以上 → 年份 −1（跨年）
  assert feishu.parse_event_time('【发生时间】12月30日 23:10', ref) == \
    _ts(2025, 12, 30, 23, 10, 0)


def test_label_normalization():
  want = _ts(2026, 10, 5, 1, 45, 4)
  for text in (
    '【 发生时间 】：2026年10月5日01时45分04秒',
    '［发生时间］2026年10月5日01:45:04',
    '发生时间：2026年10月5日01:45:04',
    '【发生时间】\n2026年10月5日01:45:04',
  ):
    assert feishu.parse_event_time(text) == want, text


def test_report_value_and_room():
  assert feishu.extract_report_value('【告警值】:9.25（已编辑）') == 9.25
  assert feishu.extract_report_value('【告警值】9.25V') == 9.25
  assert feishu.extract_report_value('【告警值】：-12.5') == -12.5
  assert feishu.extract_report_value('【告警值】:abc') is None
  assert feishu.extract_report_value('无') is None

  assert feishu.extract_room_code('【地点】廊坊A12-M1-204') == 'M1-204'
  assert feishu.extract_room_code('【事件概述】m1-204 机房漏水') == 'M1-204'
  assert feishu.extract_room_code('无编码') == ''


def test_ewatch_report():
  """eWatch 推送：认速报、行首「时间」可取、只按时间+告警值参与匹配、不抽机房编码。"""
  assert feishu.is_ewatch(_EWATCH)
  assert not feishu.is_ewatch('【RZ-LF-A12数据中心速报】')
  want = _ts(2026, 10, 20, 16, 40, 44)
  assert feishu.parse_event_time(_EWATCH) == want
  # 标签锚定：同一条里后出现「恢复时间：」不会被拾起（不能全文搜「时间」）
  assert feishu.parse_event_time(_EWATCH + '\n恢复时间：2026-10-20 17:10:00') == want
  assert feishu.extract_report_value(_EWATCH) == 3366.0
  # `A6-M2-4F-401` / `rz6241-4b-01-03` 会被 `_ROOM_RE` 抽成垃圾码 → 一律不抽
  assert feishu.extract_room_code(_EWATCH) == ''

  row = feishu.build_report_row(
    chat_id='oc_a', message_id='om_e', create_s=want + 30, text=_EWATCH,
    sender='机器人', sender_id='cli_x', sender_id_type='app_id', msg_type='text',
  )
  assert (row['kind'], row['event_time'], row['report_value'], row['room_code']) == \
    ('sos', want, 3366.0, '')
  assert row['title'] == '华为'

  # 无告警：事件名称/类型留空，速报原文进末两列
  entry = build._compose({'reports': [dict(row, cluster='廊坊')]}, None, {}, 1)
  assert entry['事件名称'] == '' and entry['事件类型'] == ''
  assert entry['速报内容'] == _EWATCH and entry['首报内容'] == ''
  assert entry['地点'] == '廊坊'
  assert len(entry) == len(build.COLUMNS) == 22


def test_ewatch_single_line_time():
  """单行形态：`时间：` 截到下一个字段标签为止，且不把描述里的日期当成发生时间。"""
  want = _ts(2026, 10, 29, 16, 7, 0)
  assert feishu.is_ewatch(_EWATCH_SINGLE)
  assert feishu.classify(_EWATCH_SINGLE) == 'sos'
  assert feishu.parse_event_time(_EWATCH_SINGLE) == want
  assert feishu.extract_report_value(_EWATCH_SINGLE) == 37.27
  # 同行带「恢复时间：」也不影响（否定环视 + 锚定发生时刻）
  assert feishu.parse_event_time(
    _EWATCH_SINGLE + ' 恢复时间：2026-10-29 17:14:36') == want
  # 描述里有「预计 2026年10月30日 完成」→ 必须在描述标签处截断，不能被 _DT_YMD 吃掉
  assert '2026年10月30日' in _EWATCH_SINGLE
  assert feishu.parse_event_time(_EWATCH_SINGLE) == want

  row = feishu.build_report_row(
    chat_id='oc_a', message_id='om_s', create_s=want + 4020, text=_EWATCH_SINGLE,
    sender='机器人', sender_id='cli_x', sender_id_type='app_id', msg_type='text',
  )
  assert row['kind'] == 'sos' and row['event_time'] == want

  # 发生时间取到真值 → 与速报时间不再相同
  entry = build._compose({'reports': [dict(row, cluster='廊坊')]}, None, {}, 1)
  assert entry['发生时间'] == rules.fmt_event_time(want)
  assert entry['速报时间'] == rules.fmt_event_time(want + 4020)
  assert entry['速报内容'] == _EWATCH_SINGLE


def test_extract_normalize_and_apply():
  """模型兜底：只填空字段、只收合理区间内的时刻（识别不出来的不许乱填）。"""
  base = _ts(2026, 10, 29, 17, 14, 36)
  row = {'kind': 'sos', 'message_id': 'om_1', 'text': 'x', 'create_time': base,
         'event_time': None, 'report_value': None}

  # 合法时刻 + 告警值 → 填上
  norm = extract.normalize(row, {'event_time': '2026-10-29 16:07:00', 'report_value': 37.27})
  assert norm == {'t': _ts(2026, 10, 29, 16, 7, 0), 'v': 37.27}
  copy = dict(row)
  assert extract.apply(copy, norm) is True
  assert copy['event_time'] == _ts(2026, 10, 29, 16, 7, 0)
  assert copy['report_value'] == 37.27

  # 已有值不覆盖
  filled = dict(copy)
  assert extract.apply(filled, {'t': base - 3600, 'v': 1.0}) is False
  assert filled['event_time'] == _ts(2026, 10, 29, 16, 7, 0)
  assert filled['report_value'] == 37.27

  # 非法/越界时刻一律丢弃：非法日期、超出 7 天、晚于消息 5 分钟以上
  for bad in ('2026年13月40日10时00分00秒', '不是时间', '',
              '2026-10-01 09:00:00', '2026-10-29 18:00:00'):
    assert extract.normalize(row, {'event_time': bad})['t'] == 0, bad
  # 模型不返回 / 返回 null → 什么都没有
  assert extract.normalize(row, {'event_time': None, 'report_value': None}) == {'t': 0, 'v': None}

  # 告警值非法（非数字 / 离谱量级）
  assert extract.normalize(row, {'report_value': 'abc'})['v'] is None
  assert extract.normalize(row, {'report_value': 1e12})['v'] is None
  assert extract.normalize(row, {'report_value': True})['v'] is None

  # other 类型不进统计表 → 永远不值得调模型
  assert extract.needs_llm({'kind': 'other', 'event_time': None}) is False
  assert extract.needs_llm({'kind': 'sos', 'event_time': None}) is True
  # 速报缺告警值也值得补；首报只关心发生时间
  assert extract.needs_llm({'kind': 'sos', 'event_time': 1, 'report_value': None}) is True
  assert extract.needs_llm({'kind': 'first', 'event_time': 1, 'report_value': None}) is False


def test_build_rows_blank_occurred_when_unknown():
  """报文与告警都没有发生时间：行仍按速报时间归档，但「发生时间」留空、不判违规。"""
  from service.scheduled_task.incident_stats import source

  create_s = _ts(2026, 10, 29, 17, 14, 36)
  old = (source.load_reports, source.load_alarms, source.load_spaces)
  report = _report('sos', create_s, text='报文里没有时间，也匹配不到告警')
  report['event_time'] = None                   # _report 缺省会回填 create_time，这里要真 NULL
  source.load_reports = lambda *a, **k: [report]
  source.load_alarms = lambda guids: {}
  source.load_spaces = lambda: []
  try:
    rows = build.build_rows(create_s - 60, create_s + 60, '廊坊', ['oc_a'])
  finally:
    source.load_reports, source.load_alarms, source.load_spaces = old

  assert len(rows) == 1
  row = rows[0]
  assert row['日期'] == date(2026, 10, 29)      # 日期照常用速报当天填
  assert row['发生时间'] == ''                  # 不写推测时刻
  assert row['速报时间'] == rules.fmt_event_time(create_s)
  assert row['过程有无违规'] == ''              # 发生时间不明 → 不判违规
  assert len(row) == len(build.COLUMNS) == 22


def test_card_report():
  """卡片式事件通报：关键字认首报、冒号写在【】内也能取值、垃圾机房码不参与匹配。"""
  want = _ts(2026, 10, 29, 3, 25, 4)
  assert feishu.parse_event_time(_CARD) == want
  # 【发生时间：】必须取到干净值（脏前缀 `] : ` 会让时间解析失败）
  assert feishu._labeled(_CARD, ('发生时间',)) == '2026年10月29日03时25分04秒'
  # `A1M1-3F` 里抽出的 `M1-3` 是垃圾码，一旦当机房信号会误命中 M1-301 之类
  assert feishu.extract_room_code(_CARD) == ''
  assert feishu.extract_room_code('【地点】廊坊A12-M1-204') == 'M1-204'
  assert feishu.extract_report_value(_CARD) is None

  assert build._report_field(_CARD, ('事件概述', '事件名称')).startswith('3F-B配电室-A1M1')
  assert build._report_field(_CARD, ('地点', '影响范围')) == '3F-B配电室'

  row = feishu.build_report_row(
    chat_id='oc_cq', message_id='om_c', create_s=_ts(2026, 10, 29, 3, 30, 0),
    text=_CARD, msg_type='interactive', raw_body='{}',
  )
  assert (row['kind'], row['event_time'], row['room_code']) == ('first', want, '')

  # 无告警：名称取【事件概述】、地点取【影响范围】，不是退回园区名/原文列
  entry = build._compose({'reports': [dict(row, cluster='重庆')]}, None, {}, 1)
  assert entry['事件名称'] == '3F-B配电室-A1M1-3F-B配电室-UPS-B3U5-UPS正常模式-告警'
  assert entry['地点'] == '3F-B配电室'
  assert entry['速报内容'] == '' and entry['首报内容'] == ''
  assert entry['首报时间'] == rules.fmt_event_time(_ts(2026, 10, 29, 3, 30, 0))
  assert entry['过程有无违规'] == '缺速报'
  assert len(entry) == len(build.COLUMNS) == 22


_CARD_PROGRESS = '1.现场人员确认为UPS逆变器温度高告警'  # 只取最新一条追加描述

# 现场 B1M2 卡片逐字样例（2026-11-05 用户提供）：原地字段【进展同步】 + 末尾追加
# `进展同步1 /【进展描述】`。截断只切追加块，前面的影响范围等字段必须保留。
_B1M2 = (
  '【RZ-LF-B1M2事件通报】　【发生时间】 2026年11月05日13时50分16秒'
  '【进展同步】 B1M2-4F-402机房-1-1#列头柜B路A-03机柜基础侧供电正常，现场查看为小电流波动'
  '【事件来源】 动环告警【涉及客户】 [字节]'
  '【影响范围】 B1M2-4F-402机房-1-1#列头柜B路A-03机柜'
  '【冗余配置】 2N【系统状态】 基础侧供电正常，设备运行正常'
  '－－－－－－－－－－－－－－进展同步1'
  '【通报时间】 2026年11月05日13时54分19秒'
  '【进展描述】 已上报值班工程师、专业工程师、分部部长、客服、慧运维'
)


# 真实廊坊群卡片（2026-11-09）：字段是**换行拆开的裸标签**，块 2 起没有标签，
# 末尾「事件结束」段带最终处理结果（它才是这次事件的最终进展）。
_REAL_A18 = (
  '【RZ-LF-A18数据中心事件通报】\n【\n发生时间\n】\n2026年11月09日13时50分06秒\n'
  '进展同步\nA18数据中心3F1#强电间湿度过低报警，人员已前往现场处理\n'
  '事件来源\n动环告警\n涉及客户\n[未影响客户]\n影响范围\n3F1#强电间\n'
  '系统状态\n现场设备运行正常，湿度17.8%\n'
  '－－－－－－－－－－－－－－\n'
  '进展同步1\n通报时间\n2026年11月09日13时52分30秒\n进展描述\n'
  '现场设备运行正常，人员正在进行加湿处理\n'
  '进展同步2\n2026年11月09日13时54分46秒\n湿度正在上升，现场18.5%。\n'
  '进展同步3\n2026年11月09日13时56分26秒\n人员正在持续观察\n'
  '事件结束\n填写人\n赵云翔\n处理人\n程宏博\n处理时间\n'
  '2026年11月09日13时50分25秒\n至\n2026年11月09日13时57分59秒\n'
  '处理结果\n现场已处理,告警已解除\n事件原因\n环境湿度波动导致'
)

# 事件仍在进行（同一天 A2）：18 块、**没有事件结束** → 取编号最大那块
_REAL_A2 = ('【RZ-LF-A2数据中心事件通报】\n进展同步\nA2数据中心M2模组动力B1-1UPS风扇故障\n'
            '－－－－－－－－－－－－－－\n')
for _i in range(1, 19):
  _real_desc = ('故障UPS外壳已回装完成，准备开机测试。' if _i == 18
                else f'第{_i}步处置完成')
  _REAL_A2 += f'进展同步{_i}\n2026年11月09日10时{_i:02d}分00秒\n{_real_desc}\n'

# 同一卡片全挤在一行的形态（现场既有换行版也有单行版）
_REAL_SINGLE = (
  '【RZ-LF-A18数据中心事件通报】 进展同步 x 事件来源 动环告警 '
  '－－－－－－－－－－－－－－进展同步1 通报时间 2026年11月09日13时52分30秒 '
  '进展描述 现场设备运行正常 进展同步2 2026年11月09日13时54分46秒 湿度正在上升 '
  '事件结束 填写人 赵云翔 处理结果 现场已处理,告警已解除 事件原因 环境湿度波动'
)

# 库里存的原文（`sync.refresh_cards` 后）：lark_md/HTML 标记原样留着。
# 未清理时「处理结果」列会显示 `**】 <font color='grey'>现场已处理,告警已解除</font>`。
_MARKUP_A18 = (
  "【**发生时间**】 <font color='grey'>2026年11月09日13时50分06秒</font>\n"
  "【**进展同步**】 <font color='grey'>A18数据中心3F1#强电间湿度过低报警</font>\n"
  '－－－－－－－－－－－－－－\n'
  "**<font color='yellow-350'>进展同步1</font>**\n"
  "【**通报时间**】 <font color='grey'>2026年11月09日13时52分30秒</font>\n"
  "【**进展描述**】 <font color='grey'>现场设备运行正常，人员正在进行加湿处理</font>\n"
  '－－－－－－－－－－－－－－\n'
  "**<font color='green'>事件结束</font>**\n"
  "【**填写人**】 <font color='grey'>赵云翔</font>\n"
  "【**处理结果**】 <font color='grey'>现场已处理,告警已解除</font>\n"
  "【**事件原因**】 <font color='grey'>环境湿度波动导致</font>\n"
)

# 事件未结束的带标记形态：取编号最大的块，块体里的标签也要能剥掉
_MARKUP_A2 = (
  "**<font color='yellow-350'>进展同步18</font>**\n"
  "【**通报时间**】 <font color='grey'>2026年11月09日14时02分27秒</font>\n"
  "【**进展描述**】 <font color='grey'>故障UPS外壳已回装完成，准备开机测试。</font>\n"
  '－－－－－－－－－－－－－－\n'
  "**<font color='yellow-350'>进展同步19</font>**\n"
  "【**通报时间**】 <font color='grey'>2026年11月09日14时16分26秒</font>\n"
  "【**进展描述**】 <font color='grey'>UPS已开机，厂家已测试完成</font>\n"
)


def test_card_progress():
  """「事件进展」取最新追加块；有「事件结束」时取它的最终处理结果。原地【进展同步】不算。"""
  assert build._latest_progress(_CARD) == _CARD_PROGRESS
  # 只有原地【进展同步】、没有追加块 → 不进「事件进展」
  inline_only = '【RZ-LF-A1事件通报】\n【进展同步】 基础侧供电正常\n【事件来源】 动环告警\n'
  assert build._latest_progress(inline_only) == ''
  # 最后一段为空时回退到上一段非空
  assert build._latest_progress('【进展描述】 甲【进展描述】 ') == '甲'

  # 真实廊坊群格式：裸标签 / 块 2 起无标签 / 事件结束的最终处理结果
  assert build._latest_progress(_REAL_A18) == '现场已处理,告警已解除'
  assert build._latest_progress(_REAL_SINGLE) == '现场已处理,告警已解除'
  # 事件未结束（18 块）→ 取编号最大的那条，不是编号 1
  assert build._latest_progress(_REAL_A2) == '故障UPS外壳已回装完成，准备开机测试'
  # 入表后「事件进展」不再是告警状态兜底的「已解除」
  entry = build._compose({'reports': [_report('first', _T0 + 300, text=_REAL_A2)]},
                         None, {}, 1)
  assert entry['事件进展'] == '故障UPS外壳已回装完成，准备开机测试'

  # 库里存的卡片原文带 lark_md/HTML 标记（`**`、`<font color='grey'>`）：
  # 取值必须只留正文，不能把标签一起写进「事件进展」「处理结果」
  assert build._latest_progress(_MARKUP_A18) == '现场已处理,告警已解除'
  assert build._latest_progress(_MARKUP_A2) == 'UPS已开机，厂家已测试完成'
  assert build._report_field(_MARKUP_A18, ('发生时间',)) == '2026年11月09日13时50分06秒'
  entry = build._compose({'reports': [_report('first', _T0 + 300, text=_MARKUP_A18)]},
                         None, {}, 1)
  assert entry['事件进展'] == '现场已处理,告警已解除'
  assert entry['处理结果'] == ''          # 无告警仍留空，语义不变

  row = feishu.build_report_row(
    chat_id='oc_cq', message_id='om_c', create_s=_ts(2026, 10, 29, 3, 30, 0),
    text=_CARD, msg_type='interactive', raw_body='{}',
  )
  entry = build._compose({'reports': [dict(row, cluster='重庆')]}, None, {}, 1)
  assert entry['事件进展'] == _CARD_PROGRESS
  assert entry['处理结果'] == ''            # 无告警仍留空，语义不变

  alarm = _alarm(order_no='WO9', order_status='2', process='电池老化',
                 process_result='2', start_time=_T0, end_time=_T0 + 3660)
  entry = build._compose({'reports': [dict(row, cluster='重庆')]}, alarm, {}, 1)
  assert entry['事件进展'] == _CARD_PROGRESS  # 值守手写进展优先
  assert entry['处理结果'] == '已恢复正常'    # 工单状态仍单独给出


def test_progress_blocks():
  """进展全记录：按编号列块（同号取后者）、旧格式兜底、「事件结束」段字段。"""
  blocks = build.progress_blocks(_MARKUP_A2)
  assert [b['no'] for b in blocks] == [18, 19]
  assert blocks[0]['time'] == '2026年11月09日14时02分27秒'
  assert blocks[0]['desc'] == '故障UPS外壳已回装完成，准备开机测试。'
  assert blocks[1]['desc'] == 'UPS已开机，厂家已测试完成'

  # 同号取后出现者（同组多条报文各自从 1 起编号）
  dup = ('进展同步1 2026年11月09日10时00分00秒 甲\n'
         '－－－－－－－－\n'
         '进展同步1 2026年11月09日10时05分00秒 乙\n'
         '进展同步2 2026年11月09日10时10分00秒 丙')
  assert [(b['no'], b['desc']) for b in build.progress_blocks(dup)] == [(1, '乙'), (2, '丙')]

  # 旧格式兜底：没有编号块时默认不列，include_legacy 才按出现顺序列出
  legacy = '【进展描述】 甲\n【进展描述】 乙'
  assert build.progress_blocks(legacy) == []
  assert [b['desc'] for b in build.progress_blocks(legacy, include_legacy=True)] == ['甲', '乙']

  # 「事件结束」段：处理结果/原因/填写人；没有该段返回空
  final = build.final_section(_MARKUP_A18)
  assert final['处理结果'] == '现场已处理,告警已解除'
  assert final['事件原因'] == '环境湿度波动导致'
  assert final['填写人'] == '赵云翔'
  assert build.final_section(_MARKUP_A2) == {}


# 用户 2026-11-10 反馈的原样形态（单行卡片 + 事件结束 + 【**处理结果**】）：
# 未去标记时「事件进展」显示 `**】 <font color='grey'>现场已处理,告警已解除</font>`。
_USER_SAMPLE = (
  '【RZ-LF-A2事件通报】\n进展同步1\n2026年11月09日13时52分30秒\n'
  '池室A - M1-2F-A电池室A2-402 - 第07节电池温度 - 温度过低报警\n'
  "事件结束\n【**处理结果**】 <font color='grey'>现场已处理,告警已解除</font>\n"
)


def test_markup_never_leaks():
  """卡片标记（`**` / `<font>`）不能以任何形态进表格 —— 含用户反馈的原样样例。"""
  for value in (build._latest_progress(_USER_SAMPLE),
                build._final_result(_USER_SAMPLE),
                build.final_section(_USER_SAMPLE)['处理结果']):
    assert value == '现场已处理,告警已解除', value

  # 捕获点落在标签中间（`**】 <font …>`）也要剥干净
  assert build._clean_value("**】 <font color='grey'>现场已处理</font>") == '现场已处理'

  # 整行 22 列都不许残留标记（不只「事件进展」「处理结果」两列）
  entry = build._compose({'reports': [_report('first', _T0 + 300, text=_MARKUP_A18)]},
                         None, {}, 1)
  assert len(entry) == len(build.COLUMNS) == 22
  for column, value in entry.items():
    if isinstance(value, str):
      assert '**' not in value and '<font' not in value and '</' not in value, (column, value)

  # 裸标签 / 旧格式兜底同样自足清理，不依赖调用方先过 _plain_text
  assert build._bare_field("事件结果\n【**处理结果**】 <font color='grey'>甲</font>", '处理结果') == ''
  assert build._legacy_progress("【**进展描述**】 <font color='grey'>乙</font>") == '乙'

  # 幂等：重复清理不会继续改变文本
  once = build._plain_text(_MARKUP_A18)
  assert build._plain_text(once) == once
  assert build._clean_value(build._clean_value(_USER_SAMPLE)) == build._clean_value(_USER_SAMPLE)


def _event(title, place, text=''):
  return {'title': title, 'place': place,
          'haystack': build._plain_text(f'{title} {place} {text}').lower()}


def test_event_search():
  """事件搜索：空白/逗号切词、全部命中（AND）、大小写不敏感、空 query 返回全部。"""
  events = [
    _event('廊坊园区A2数据中心 - UPS - 电池 - 故障告警', 'A2 1F电池室', '电池内阻偏高'),
    _event('廊坊园区A2数据中心 - 空调 - 回风温度 - 过高告警', 'A2 3F机房', '温度过高'),
    _event('廊坊园区A18数据中心 - UPS - 输出 - 故障告警', 'A18 1F强电间', 'UPS故障'),
  ]
  assert len(detail.search(events, 'A2 UPS')) == 1
  assert len(detail.search(events, 'A2、UPS')) == 1       # 顿号也算分隔
  assert len(detail.search(events, 'a2 ups')) == 1        # 大小写不敏感
  assert len(detail.search(events, 'A2')) == 2
  assert detail.search(events, 'A2 湿度过低') == []
  assert len(detail.search(events, '')) == 3
  assert detail.search(events, 'A18 UPS')[0]['title'].startswith('廊坊园区A18')


def test_strip_progress():
  """仲裁输入只截追加块：原地字段（含影响范围）必须保留。"""
  stripped = arbitrate._strip_progress(_B1M2)
  assert stripped.endswith('基础侧供电正常，设备运行正常')
  for field in ('【事件来源】', '【涉及客户】', '【影响范围】', '【冗余配置】',
                '【系统状态】', '【进展同步】'):
    assert field in stripped
  assert '进展描述' not in stripped and '已上报值班工程师' not in stripped

  # 无追加块 → 原样返回
  assert arbitrate._strip_progress('【事件概述】 电池内阻低') == '【事件概述】 电池内阻低'

  # 进入模型仲裁的提示词同样不含追加的进展
  _, user = arbitrate.build_prompt({'text': _CARD, 'event_time': _T0}, [])
  payload = json.loads(user)['通报']
  assert '进展描述' not in payload['通报原文']
  assert '人员已前往现场应急' not in payload['通报原文']
  assert '【事件概述：】' in payload['通报原文']


def _cand(eid, hits, detail=''):
  return {'event_log_id': eid, 'hits': hits, 'detail': detail}


def test_pick_candidate():
  # 同秒单候选 → 配，detail 照实（时间已是必需项，不再有 `single` 低置信说法）
  assert sync._pick_candidate([_cand('E1', 1, 'time')]) == ('E1', 1, 'time')
  assert sync._pick_candidate([_cand('E1', 2, 'time+value')]) == ('E1', 2, 'time+value')
  # 同秒多条 + 无告警值 → 收不窄，交模型（文本不参与取舍）
  assert sync._pick_candidate([
    _cand('E1', 1, 'time'), _cand('E2', 1, 'time')]) is None
  # 同秒多条 + 报文有告警值 → 只收窄到值命中的那条
  assert sync._pick_candidate(
    [_cand('E1', 2, 'time+value'), _cand('E2', 1, 'time')], 9.25) == ('E1', 2, 'time+value')
  # 值全配上 / 全没配上（单位格式差异）→ 不反向否决，仍交模型
  assert sync._pick_candidate([
    _cand('E1', 2, 'time+value'), _cand('E2', 2, 'time+value')], 9.25) is None
  assert sync._pick_candidate([
    _cand('E1', 1, 'time'), _cand('E2', 1, 'time')], 9.25) is None
  assert sync._pick_candidate([]) is None


def test_narrow_by_value():
  """同秒多候选：报文有告警值才收窄；收窄为空不反向否决；机房不再参与。"""
  cands = [_cand('E1', 2, 'time+value'), _cand('E2', 1, 'time')]
  assert [c['event_log_id'] for c in sync._narrow_by_value(cands, 9.25)] == ['E1']
  # 报文没有告警值 → 原样返回，只按时间
  assert [c['event_log_id'] for c in sync._narrow_by_value(cands, None)] == ['E1', 'E2']
  # 谁都对不上（单位/格式差异）→ 不反向否决
  only_time = [_cand('E1', 1, 'time'), _cand('E2', 1, 'time')]
  assert [c['event_log_id'] for c in sync._narrow_by_value(only_time, 9.25)] == ['E1', 'E2']
  # 并列组按 event_log_id 升序，只为编号稳定，不参与取舍（反序输入验证排序生效）
  ordered = sync._tied_group([_cand('E2', 1, 'time'), _cand('E1', 1, 'time')])
  assert [c['event_log_id'] for c in ordered] == ['E1', 'E2']


def _arb_cand(eid, device, space='M1-204 机房', content='电压过低'):
  return dict(_cand(eid, 2, 'time+value'), trigger_time=_T0, value=9.25, space=space,
              device_name=device, point_name='CA线电压', content=content, level='重要')


def test_arbitration_prompt():
  """提示词必须带通报原文与全部候选，序号从 1 起，超长原文截断。"""
  text = ('【发生时间】2026-10-12 10:00:00\n【告警值】9.25\n'
          '【地点】廊坊A12-M1-204\n【事件概述】市电B路电表CA线电压过低')
  report = dict(_report('sos', _T0), report_value=9.25, text=text)
  cands = [_arb_cand('E1', '市电B路电表'), _arb_cand('E2', '市电A路电表')]

  system, user = arbitrate.build_prompt(report, cands)
  assert 'pick' in system and 'null' in system
  payload = json.loads(user)
  assert payload['通报']['通报原文'] == text
  # 机房抽取结果不喂模型（抽不到或抽出垃圾码会带偏仲裁）
  assert '通报机房编码' not in payload['通报']
  assert payload['通报']['通报告警值'] == 9.25
  assert [c['序号'] for c in payload['候选告警']] == [1, 2]
  assert payload['候选告警'][0]['设备'] == '市电B路电表'
  assert payload['候选告警'][1]['设备'] == '市电A路电表'
  assert payload['候选告警'][0]['触发告警值'] == 9.25

  long_report = dict(report, text='长' * 1000)
  assert len(json.loads(arbitrate.build_prompt(long_report, cands)[1])
             ['通报']['通报原文']) == 800


def test_parse_pick():
  assert arbitrate.parse_pick('{"pick": 2, "reason": "B路电表"}', 3) == 2
  assert arbitrate.parse_pick('{"pick": "1"}', 3) == 1
  assert arbitrate.parse_pick('{"pick": null, "reason": "证据不足"}', 3) is None
  # 代码块 / 前后夹带说明文字
  assert arbitrate.parse_pick('```json\n{"pick": 3}\n```', 3) == 3
  assert arbitrate.parse_pick('我认为是第 2 条 {"pick": 2}', 3) == 2
  # 越界 / 非数字 / 空 → 留空
  assert arbitrate.parse_pick('{"pick": 4}', 3) is None
  assert arbitrate.parse_pick('{"pick": 0}', 3) is None
  assert arbitrate.parse_pick('随便说说', 3) is None
  assert arbitrate.parse_pick('', 3) is None


def test_all_identical():
  assert arbitrate.all_identical([_arb_cand('E1', '同一个'), _arb_cand('E2', '同一个')])
  assert not arbitrate.all_identical([_arb_cand('E1', '设备甲'), _arb_cand('E2', '设备乙')])
  assert not arbitrate.all_identical([_arb_cand('E1', '设备甲')])


def test_signals():
  report = dict(_report('sos', _T0), report_value=9.25, text=_MESSAGE)
  alarm = {'trigger_time': _T0, 'trigger_value': 9.25,
           'device_name': '市电B路电表', 'point_name': 'CA线电压', 'content': '电压过低'}
  hits, detail = sync._signals(report, alarm)
  assert (hits, detail) == (2, 'time+value')
  # 机房码不参与匹配：即使报文里的码能对上空间名，也不产生 room 信号
  assert sync._signals(dict(report, room_code='M1-204'), alarm)[1] == 'time+value'
  # 时间必须完全一致：差 1 秒也不算 time 命中（用户 2026-11-04 口径）
  hits, detail = sync._signals(report, {'trigger_time': _T0 + 1, 'trigger_value': 9.25})
  assert 'time' not in detail.split('+') and hits == 1 and detail == 'value'
  # 值超出容差 → 不算命中
  hits, detail = sync._signals(report, {'trigger_time': _T0, 'trigger_value': 10.0})
  assert 'value' not in detail.split('+') and hits == 1 and detail == 'time'
  # 报文没解析出发生时间 → 没有 time 信号（不拿 create_time 兜底）
  hits, detail = sync._signals(dict(report, event_time=None), alarm)
  assert detail == 'value' and hits == 1


def test_incident_tool_window():
  """对话入口 `incident_report` 的窗口 / 园区解析（工具本身只做编排，口径在上面各用例）。"""
  from tools import incident_tools as it

  # 班次：白班 08:30–17:30，晚班 17:30–次日 08:30；00:00–08:30 仍算前一晚班
  start, end, label = it._current_shift(datetime(2026, 10, 22, 9, 42, tzinfo=_TZ))
  assert (label, f'{start:%m-%d %H:%M}', f'{end:%m-%d %H:%M}') == \
    ('白班', '10-22 08:30', '10-22 17:30')
  start, end, label = it._current_shift(datetime(2026, 10, 22, 18, 5, tzinfo=_TZ))
  assert (label, f'{start:%m-%d %H:%M}', f'{end:%m-%d %H:%M}') == \
    ('晚班', '10-22 17:30', '10-23 08:30')
  start, end, label = it._current_shift(datetime(2026, 10, 23, 3, 0, tzinfo=_TZ))
  assert (label, f'{start:%m-%d %H:%M}', f'{end:%m-%d %H:%M}') == \
    ('晚班', '10-22 17:30', '10-23 08:30')

  # 换班边界：08:29 仍是前一晚班，08:30 进白班；17:29 白班，17:30 进晚班
  for hour, minute, want in ((8, 29, '晚班'), (8, 30, '白班'),
                             (17, 29, '白班'), (17, 30, '晚班')):
    assert it._current_shift(datetime(2026, 10, 22, hour, minute, tzinfo=_TZ))[2] == want

  # 都不传 = 当前班次（白班 9h / 晚班 15h）
  now_start, now_end, _ = it._current_shift()
  start_s, end_s, label = it._window('', '')
  assert (start_s, end_s) == (int(now_start.timestamp()), int(now_end.timestamp()))
  assert '~' in label

  # 日期含首尾：20~22 共 3 个自然日
  start_s, end_s, label = it._window('2026-10-20', '2026-10-22')
  assert end_s - start_s == 3 * 86400 and label.startswith('2026-10-20 00:00')
  # 只传 start → 7 个自然日
  start_s, end_s, _ = it._window('2026-10-20', '')
  assert end_s - start_s == 7 * 86400
  # 带时分 → 精确到分（左闭右开）
  start_s, end_s, _ = it._window('2026-10-21 08:00', '2026-10-21 18:00')
  assert end_s - start_s == 10 * 3600

  for args in (('2026-10-23', '2026-10-20'), ('2026-13-01', ''), ('', '2026-10-22'),
               ('2026-10-20 08:00', ''), ('2026-10-20', '2026-10-21 08:00')):
    try:
      it._window(*args)
    except ValueError:
      continue
    raise AssertionError(f'未拒绝非法窗口 {args}')


def test_incident_shift_window():
  """班次语义 → 窗口（`shift`/`date`）：对话里「今天/昨天/白班/晚班」不再按自然日。

  2026-11-06 用户反馈「事件统计表按班次，为什么对话里是按一整天」——模型原来只能传裸日期
  （语义是自然日 00:00–24:00），现在只传语义，窗口由服务端算。
  """
  from tools import incident_tools as it

  def win(shift, date_str, now):
    start, end, name = it._resolve_shift(shift, date_str, now)
    return name, f'{start:%m-%d %H:%M}', f'{end:%m-%d %H:%M}'

  def at(hour, minute=0):
    return datetime(2026, 11, 6, hour, minute, tzinfo=_TZ)

  # 都不传 / current / today = 当前班次（00:00–08:30 仍算前一晚班）
  for shift in ('', 'current', 'today'):
    assert win(shift, '', at(10)) == ('白班', '11-06 08:30', '11-06 17:30')
    assert win(shift, '', at(19)) == ('晚班', '11-06 17:30', '11-07 08:30')
    assert win(shift, '', at(3)) == ('晚班', '11-05 17:30', '11-06 08:30')

  # day / night 缺省日期 = 最近一次**已开始**的该班次（永不返回未来空窗口）
  assert win('day', '', at(10)) == ('白班', '11-06 08:30', '11-06 17:30')
  assert win('day', '', at(7)) == ('白班', '11-05 08:30', '11-05 17:30')
  assert win('night', '', at(10)) == ('晚班', '11-05 17:30', '11-06 08:30')
  assert win('night', '', at(3)) == ('晚班', '11-05 17:30', '11-06 08:30')
  assert win('night', '', at(19)) == ('晚班', '11-06 17:30', '11-07 08:30')

  # 指定日期
  assert win('day', '2026-11-03', at(10)) == ('白班', '11-03 08:30', '11-03 17:30')
  assert win('night', '2026-11-03', at(10)) == ('晚班', '11-03 17:30', '11-04 08:30')

  # yesterday = 昨天白班 + 晚班（昨天 08:30 ~ 今天 08:30），凌晨问也是同一窗口
  assert win('yesterday', '', at(10)) == ('白班+晚班', '11-05 08:30', '11-06 08:30')
  assert win('yesterday', '', at(3)) == ('白班+晚班', '11-05 08:30', '11-06 08:30')

  # last = 最近一个已结束的班次
  assert win('last', '', at(10)) == ('晚班', '11-05 17:30', '11-06 08:30')
  assert win('last', '', at(3)) == ('白班', '11-05 08:30', '11-05 17:30')
  assert win('last', '', at(19)) == ('白班', '11-06 08:30', '11-06 17:30')

  # 只给日期 = 该日白班+晚班（时段仍按班次切）
  assert win('', '2026-11-05', at(10)) == ('白班+晚班', '11-05 08:30', '11-06 08:30')

  # _window 走同一套，返回 epoch 秒；文案首段是班次名
  start_s, end_s, label = it._window('', '', 'night', '2026-11-03')
  assert (start_s, end_s) == (int(datetime(2026, 11, 3, 17, 30, tzinfo=_TZ).timestamp()),
                              int(datetime(2026, 11, 4, 8, 30, tzinfo=_TZ).timestamp()))
  assert label.startswith('晚班 ') and it._label_shift(label) == '晚班'
  assert it._label_shift('2026-11-03 00:00 ~ 2026-11-04 00:00') == ''

  # shift/date 与 start/end 互斥；date 只配 day/night；值必须合法
  for args in (('2026-11-06', '', 'day', ''),
               ('', '2026-11-07', '', '2026-11-06'),
               ('2026-11-06', '2026-11-07', 'night', '')):
    try:
      it._window(*args)
    except ValueError:
      continue
    raise AssertionError(f'未拒绝 shift/date 与 start/end 混用 {args}')

  for shift, date_str in (('current', '2026-11-05'), ('today', '2026-11-05'),
                          ('last', '2026-11-05'), ('yesterday', '2026-11-05'),
                          ('week', ''), ('day', '2026-13-01'),
                          ('night', '2026/11/05')):
    try:
      it._resolve_shift(shift, date_str, at(10))
    except ValueError:
      continue
    raise AssertionError(f'未拒绝非法班次参数 {(shift, date_str)}')


def test_incident_tool_parks():
  from tools import incident_tools as it

  assert it._split_parks('廊坊，平湖、重庆') == ['廊坊', '平湖', '重庆']
  assert it._PARK_SUFFIX.sub('', '廊坊园区') == '廊坊'

  original = it.settings.INCIDENT_FEISHU_PARK_CHATS
  try:
    it.settings.INCIDENT_FEISHU_PARK_CHATS = '{"廊坊":"oc_a","平湖":"oc_b"}'
    assert it._resolve_parks('') == (['廊坊', '平湖'], [])
    # 口误后缀 / 多余修饰按前缀吸附
    assert it._resolve_parks('廊坊园区') == (['廊坊'], [])
    assert it._resolve_parks('廊坊,重庆') == (['廊坊'], ['重庆'])
    # 无配置时任何请求都算未命中（工具返回明确的配置错误）
    it.settings.INCIDENT_FEISHU_PARK_CHATS = ''
    assert it._resolve_parks('') == ([], [])
    assert it._resolve_parks('廊坊') == ([], ['廊坊'])
  finally:
    it.settings.INCIDENT_FEISHU_PARK_CHATS = original


def test_build_rows_scoped_to_chat():
  """出表必须把园区群 chat_id 传到取数层（同园区测试群/合成数据不得混入）。"""
  from service.scheduled_task.incident_stats import source

  captured = {}
  original = source.load_reports

  def fake_load(start_s, end_s, cluster_prefix=None, chat_ids=None):
    captured.update(cluster_prefix=cluster_prefix, chat_ids=chat_ids)
    return []

  source.load_reports = fake_load
  try:
    assert build.build_rows(_T0, _T0 + 3600, '廊坊', ['oc_a']) == []
  finally:
    source.load_reports = original
  assert captured == {'cluster_prefix': '廊坊', 'chat_ids': ['oc_a']}


def test_outside_events():
  """0 行时必须能说出「通报在窗口内、事件发生时间在窗口外」的补录事件。"""
  from service.scheduled_task.incident_stats import source

  window_start, window_end = _ts(2026, 10, 26, 8, 0), _ts(2026, 10, 26, 18, 0)
  occurred = _ts(2026, 10, 25, 22, 50, 36)      # 前一晚班 22:50，今早 09:56 才补发通报

  old_load, old_alarms = source.load_reports, source.load_alarms
  source.load_reports = lambda start_s, end_s, cluster_prefix=None, chat_ids=None: [
    _report('sos', _ts(2026, 10, 26, 9, 56, 0), event_log_id='E1', event_time=occurred),
    _report('first', _ts(2026, 10, 26, 9, 56, 19), event_log_id='E1', event_time=occurred),
  ]
  try:
    source.load_alarms = lambda guids: {'E1': _alarm(alarm_time=occurred)}
    hint = build.outside_events(window_start, window_end, '廊坊', ['oc_a'])
    assert hint['reports'] == 2 and hint['events'] == 1 and hint['truncated'] is False
    assert [i['occurred'] for i in hint['items']] == [occurred]
    assert hint['items'][0]['name'] == 'T1组-内阻低报警'
    assert hint['items'][0]['cluster'] == '廊坊A'

    # 事件发生时间就在窗口内 → 不是「窗口外事件」
    source.load_alarms = lambda guids: {'E1': _alarm(alarm_time=_ts(2026, 10, 26, 10, 0))}
    assert build.outside_events(window_start, window_end, '廊坊', ['oc_a'])['items'] == []

    # 窗口内一条通报都没有
    source.load_reports = lambda *a, **k: []
    assert build.outside_events(window_start, window_end, '廊坊', ['oc_a']) == {
      'reports': 0, 'events': 0, 'items': [], 'truncated': False,
    }
  finally:
    source.load_reports, source.load_alarms = old_load, old_alarms


def test_empty_note():
  """0 行说明必须讲清窗口/发生时间/班次/可查日期，且不再暗示消息没收到。"""
  from tools import incident_tools as it

  window_label = '白班 2026-10-26 08:30 ~ 2026-10-26 17:30'
  note = it._empty_note(window_label, {
    'reports': 2, 'events': 1, 'truncated': False,
    'items': [{'occurred': _ts(2026, 10, 25, 22, 50, 36),
               'cluster': '廊坊A', 'name': '水泵停机报警'}],
  })
  assert '白班 2026-10-26 08:30' in note
  assert '2026-10-25 22:50' in note and '晚班' in note
  assert '水泵停机报警' in note and 'shift=night date=2026-10-25' in note

  # 超过 5 起只列前几条，但报出真实总数
  truncated = it._empty_note(window_label, {
    'reports': 24, 'events': 8, 'truncated': True,
    'items': [{'occurred': _ts(2026, 10, 25, 22, 50, 36), 'cluster': '廊坊A', 'name': 'A'},
              {'occurred': _ts(2026, 10, 25, 23, 10, 0), 'cluster': '廊坊A', 'name': 'B'}],
  })
  assert '共 8 起' in truncated and '前 2 起' in truncated

  empty = it._empty_note(window_label, {'reports': 0, 'events': 0, 'items': [], 'truncated': False})
  assert '没有收到通报' in empty
  assert '或机器人尚未进群' not in empty     # 旧文案会让人以为消息丢了


def test_visible_rows_drops_empty_and_json_safe():
  """对话交付用：非核心的全空列丢弃 + date 转字符串可 JSON 序列化。

  核心列（含速报时间/首报时间）即使整列为空也保留 —— 否则模型只能写「缺首报」、
  给不出时刻（2026-10-30 用户反馈「总是缺字段」）。
  """
  from tools import incident_tools as it

  rows = [{'序号': 1, '日期': date(2026, 10, 22), '事件等级': '',
           '事件名称': 'A', '经手人': ''}]
  out = it._visible_rows(rows)
  assert '事件等级' not in out[0] and '经手人' not in out[0]
  assert out[0]['序号'] == 1 and out[0]['日期'] == '2026-10-22'
  assert out[0]['事件名称'] == 'A' and out[0]['速报时间'] is None
  json.dumps(out, ensure_ascii=False)
  assert it._visible_rows([]) == []
  # 列序按 build.COLUMNS；核心列整列空也保留，不受入参 dict 顺序影响
  assert list(out[0]) == list(it._CORE_COLUMNS)


def test_markdown_table_renders_core_columns():
  """inline 文本表由服务端渲染：核心列恒在、列序同 build.COLUMNS、空值写 —。

  模型自己拼表会把速报/首报/事件名称/事件进展删掉（2026-11-06），所以表格不再交给它。
  """
  from tools import incident_tools as it

  rows = [
    {'序号': 1, '日期': date(2026, 11, 5), '事件名称': 'A|B',
     '事件进展': '第一条\n第二条', '事件等级': '', '经手人': ''},
    {'序号': 2, '日期': date(2026, 11, 5), '事件名称': 'C'},
  ]
  visible = it._visible_rows(rows)
  columns = list(visible[0])
  table = it._markdown_table(columns, visible)
  lines = table.split('\n')
  assert len(lines) == 4

  # 列序＝build.COLUMNS 过滤后的顺序；核心列在、全空非核心列不在
  assert columns == [c for c in build.COLUMNS if c in columns]
  assert '速报时间' in columns and '首报时间' in columns
  assert '事件等级' not in columns and '经手人' not in columns

  header = [c.strip() for c in lines[0].strip('|').split('|')]
  assert header == columns
  assert lines[1] == '| ' + ' | '.join('---' for _ in columns) + ' |'

  # 第二行：管道转义、换行压成空格
  assert r'A\|B' in lines[2] and '第一条 第二条' in lines[2]

  # 第二行没有首报 → 该格写 —，但整列不能消失
  cells = [c.strip() for c in lines[3].strip('|').split('|')]
  assert cells[header.index('首报时间')] == '—'
  assert cells[header.index('速报时间')] == '—'

  assert it._markdown_table([], []) == ''
  assert it._markdown_table(columns, []) == ''


def test_realtime_row_shape():
  """实时推送与拉取**同一套行构造**：同一条消息两路产出的行必须逐字段一致。"""
  from types import SimpleNamespace

  text = ('【RZ-LF-A12数据中心速报】\n'
          '【事件概述】A12 数据中心 2 层 M1-204 UPS 逆变器故障\n'
          '【发生时间】2026 年 10 月 13 日,14:30:00\n'
          '【告警值】38.6')
  content = json.dumps({'text': text}, ensure_ascii=False)
  pushed_text = feishu.text_from_content('text', content)
  assert pushed_text == text

  message = SimpleNamespace(
    msg_type='text', body=SimpleNamespace(content=content),
    message_id='om_1', create_time='1728791400000',
    sender=SimpleNamespace(sender_name='张三', id='ou_1', id_type='open_id'),
  )
  fields = dict(chat_id='oc_a', message_id='om_1', create_s=1728791400,
                sender='张三', sender_id='ou_1', sender_id_type='open_id',
                msg_type='text', raw_body=content)
  pulled = feishu.build_report_row(text=feishu.extract_text(message), **fields)
  row = feishu.build_report_row(text=pushed_text, **fields)
  assert row == pulled

  assert row['kind'] == 'sos'
  assert row['title'] == 'RZ-LF-A12数据中心速报'
  assert row['room_code'] == 'M1-204'
  assert row['report_value'] == 38.6
  assert row['event_time'] == int(datetime(2026, 10, 13, 14, 30, tzinfo=_TZ).timestamp())
  assert set(row) == {'chat_id', 'message_id', 'create_time', 'sender', 'sender_id',
                      'sender_id_type', 'kind', 'title', 'text', 'event_time',
                      'report_value', 'room_code', 'raw'}


def test_text_from_content():
  """WS 报文里 `content` 是 JSON 字符串（不是 list API 的 `.body.content`）。"""
  card = json.dumps({'title': '速报', 'elements': [
    [{'tag': 'text', 'text': '【发生时间】2026年10月13日14时30分00秒'}]],
  }, ensure_ascii=False)
  assert feishu.text_from_content('interactive', card) == (
    '速报\n【发生时间】2026年10月13日14时30分00秒')

  post = json.dumps({'title': '通报', 'content': [
    [{'tag': 'text', 'text': '第一段'}, {'tag': 'at', 'text': '@某人'}],
    [{'tag': 'text', 'text': '第二段'}],
  ]}, ensure_ascii=False)
  assert feishu.text_from_content('post', post) == '第一段\n@某人\n第二段'

  # 解析失败退化成原文（与拉取侧一致，不丢消息）
  assert feishu.text_from_content('text', '不是 JSON') == '不是 JSON'
  assert feishu.text_from_content('text', '') == ''


def test_ingest_whitelist():
  """入库开关与白名单：空白名单是 **fail-closed**（不给「默认全收」）。"""
  from config.settings import settings
  from service.scheduled_task.incident_stats import realtime

  old = (settings.FEISHU_GROUP_INGEST_ENABLED, settings.FEISHU_GROUP_INGEST_CHATS)
  try:
    settings.FEISHU_GROUP_INGEST_CHATS = 'oc_a，oc_b , oc_c / oc_d'
    # 中文逗号认，但 `/` 不是分隔符（避免把杂字符当分隔符吞掉配置）
    assert realtime.ingest_chat_ids() == ['oc_a', 'oc_b', 'oc_c / oc_d']
    settings.FEISHU_GROUP_INGEST_ENABLED = False
    assert not realtime.should_ingest('oc_a')          # 开关关 → 一律不入库
    settings.FEISHU_GROUP_INGEST_ENABLED = True
    assert realtime.should_ingest('oc_a')
    assert realtime.should_ingest(' oc_b ')            # 两侧空白不影响
    assert not realtime.should_ingest('oc_z')          # 不在白名单
    assert not realtime.should_ingest('')              # 空 chat_id
    settings.FEISHU_GROUP_INGEST_CHATS = ''
    assert not realtime.should_ingest('oc_a')          # 空白名单 = 不启用
  finally:
    settings.FEISHU_GROUP_INGEST_ENABLED, settings.FEISHU_GROUP_INGEST_CHATS = old


def test_on_message_event_gating():
  """WS 事件取值用 `message_type`（拉取侧才是 `msg_type`）—— 写错会静默全丢。"""
  from types import SimpleNamespace

  from config.settings import settings
  from service.feishu import client
  from service.scheduled_task.incident_stats import realtime

  calls: list[dict] = []
  old_submit = client.submit_async
  old_ingest = realtime._ingest_one
  old = (settings.FEISHU_GROUP_INGEST_ENABLED, settings.FEISHU_GROUP_INGEST_CHATS)

  def fake_ingest(**kw):
    calls.append(kw)
    return object()          # 不是协程也无所谓：submit_async 已被替换

  event = SimpleNamespace(event=SimpleNamespace(
    message=SimpleNamespace(
      chat_id='oc_a', message_id='om_1', create_time='1728791400000',
      message_type='text', content='{"text":"你好"}',
    ),
    sender=SimpleNamespace(sender_id=SimpleNamespace(open_id='ou_1', user_id='')),
  ))
  try:
    client.submit_async = lambda coro: None
    realtime._ingest_one = fake_ingest
    settings.FEISHU_GROUP_INGEST_CHATS = 'oc_a'

    settings.FEISHU_GROUP_INGEST_ENABLED = False
    realtime.on_message_event(event)
    assert calls == []                       # 开关关 → 不提交

    settings.FEISHU_GROUP_INGEST_ENABLED = True
    realtime.on_message_event(event)
    assert len(calls) == 1
    assert calls[0] == {
      'chat_id': 'oc_a', 'message_id': 'om_1', 'create_s': 1728791400,
      'open_id': 'ou_1', 'msg_type': 'text', 'content': '{"text":"你好"}',
    }

    event.event.message.chat_id = 'oc_other'
    realtime.on_message_event(event)
    assert len(calls) == 1                   # 不在白名单 → 不提交
  finally:
    client.submit_async = old_submit
    realtime._ingest_one = old_ingest
    settings.FEISHU_GROUP_INGEST_ENABLED, settings.FEISHU_GROUP_INGEST_CHATS = old


def _raw_message(message_id, deleted=None, content='{"text":"现场已处理"}'):
  from types import SimpleNamespace

  body = SimpleNamespace(content=content)
  fields = dict(
    message_id=message_id, create_time='1728791400000', msg_type='text', body=body,
    sender=SimpleNamespace(sender_name='张三', id='ou_1', id_type='open_id'),
  )
  if deleted is not None:
    fields['deleted'] = deleted
  return SimpleNamespace(**fields)


def test_is_recalled():
  """撤回判定：`deleted` 优先，占位符兜底（必须先于 extract_text）。"""
  assert feishu.is_recalled(_raw_message('om_1', deleted=True))
  assert not feishu.is_recalled(_raw_message('om_1', deleted=False))
  # SDK 没回 deleted 字段时，靠占位符兜底
  assert feishu.is_recalled(_raw_message('om_1', content='This message was recalled'))
  assert not feishu.is_recalled(_raw_message('om_1'))
  assert not feishu.is_recalled(_raw_message('om_1', content=''))


def test_page_rows_skips_recalled():
  """撤回的消息不进 rows（否则会 upsert 覆盖原文），只回 message_id。"""
  rows, recalled = feishu.page_rows('oc_a', [
    _raw_message('om_ok'),
    _raw_message('om_r', deleted=True, content='This message was recalled'),
  ])
  assert [r['message_id'] for r in rows] == ['om_ok']
  assert recalled == ['om_r']
  assert all('recalled' not in (r.get('text') or '') for r in rows)

  rows, recalled = feishu.page_rows('oc_a', [])
  assert rows == [] and recalled == []


def test_on_recall_event_gating():
  """撤回事件：开关关 / 白名单外 / 空 message_id 一律不提交。"""
  from config.settings import settings
  from service.feishu import client
  from service.scheduled_task.incident_stats import realtime

  calls: list[dict] = []
  old_submit = client.submit_async
  old_delete = realtime._delete_one
  old = (settings.FEISHU_GROUP_INGEST_ENABLED, settings.FEISHU_GROUP_INGEST_CHATS)

  def fake_delete(**kw):
    calls.append(kw)
    return object()

  try:
    client.submit_async = lambda coro: None
    realtime._delete_one = fake_delete
    settings.FEISHU_GROUP_INGEST_CHATS = 'oc_a'

    settings.FEISHU_GROUP_INGEST_ENABLED = False
    realtime.on_recall_event('oc_a', 'om_1')
    assert calls == []                        # 开关关 → 不提交

    settings.FEISHU_GROUP_INGEST_ENABLED = True
    realtime.on_recall_event('oc_a', 'om_1')
    assert calls == [{'chat_id': 'oc_a', 'message_id': 'om_1'}]

    realtime.on_recall_event('oc_other', 'om_2')
    assert len(calls) == 1                    # 不在白名单 → 不提交

    realtime.on_recall_event('oc_a', '  ')
    assert len(calls) == 1                    # 空 message_id → 不提交
  finally:
    client.submit_async = old_submit
    realtime._delete_one = old_delete
    settings.FEISHU_GROUP_INGEST_ENABLED, settings.FEISHU_GROUP_INGEST_CHATS = old


async def _noop_async(*args, **kwargs):
  return 0


def test_sync_deletes_recalled():
  """sync_gaps/sync_range 把撤回的 message_id 交给 source.delete_report，且不入库。"""
  import asyncio

  saved: list[list[dict]] = []
  deleted: list[tuple[str, str]] = []

  async def fake_fetch(chat_id, start_s, end_s):
    return ([{'chat_id': chat_id, 'message_id': 'om_ok', 'create_time': 1, 'kind': 'other'}],
            ['om_r'])

  async def fake_parks():
    return {'廊坊': 'oc_a'}

  def fake_save(rows):
    saved.append(rows)
    return len(rows)

  def fake_delete(chat_id, message_id):
    deleted.append((chat_id, message_id))
    return 1

  patches = {
    (feishu, 'fetch_messages'): fake_fetch,
    (feishu, 'resolve_park_chats'): fake_parks,
    (source, 'save_reports'): fake_save,
    (source, 'delete_report'): fake_delete,
    (source, 'latest_report_time'): lambda chat_id: None,
    (sync, '_watermark'): lambda chat_id: None,
    (sync, '_set_watermark'): lambda chat_id, ts: None,
    (sync, 'match_pending'): _noop_async,
    (sync.schema, 'ensure_reports_schema'): lambda dsn: None,
  }
  originals = {(mod, name): getattr(mod, name) for mod, name in patches}
  try:
    for (mod, name), value in patches.items():
      setattr(mod, name, value)

    asyncio.run(sync.sync_gaps(3600))
    assert [r['message_id'] for r in saved[0]] == ['om_ok']   # 撤回的不入库
    assert deleted == [('oc_a', 'om_r')]

    saved.clear()
    deleted.clear()
    asyncio.run(sync.sync_range(0, None, backfill=True))
    assert [r['message_id'] for r in saved[0]] == ['om_ok']
    assert deleted == [('oc_a', 'om_r')]
  finally:
    for (mod, name), value in originals.items():
      setattr(mod, name, value)


def _assert_sql_tokens(sql: str) -> None:
  """Python 隐式拼接**不带分隔**：`' UNION ALL'` + `'SELECT ...'` 拼成 `UNION ALLSELECT`。

  2026-11-05 生产事故：`alarm_candidates` 每次抛 `syntax error at or near "ALLSELECT"`，
  `_match_pending` 第一条即中断，49 条未匹配通报永远等不到重试。纯字符串断言，不连库。
  """
  assert 'ALLSELECT' not in sql
  for i in (j for j in range(len(sql)) if sql.startswith('SELECT', j)):
    assert i == 0 or sql[i - 1].isspace(), sql[max(0, i - 20):i + 10]


def test_alarm_candidates_sql_shape():
  """候选查询必须按**触发态时间**查历史表（防回退到会被覆盖的当前态 alarm_time）。

  当前态 `dcim_alarms.alarm_time` 每条刷新/恢复推送都会重写（`parse_alarm` 全写 +
  `apply_batch` 整行 upsert），只有历史表里 `status='1'` 那条的 timestamp 不变。
  纯字符串断言，不连库。
  """
  sql = source._CANDIDATES_SQL
  assert 'dcim_alarms_history' in sql
  assert "payload->>'status' = '1'" in sql
  assert "payload->>'timestamp'" in sql
  # 老告警（没有历史行）仍走当前态兜底，但不能是唯一判据：
  # 当前态 alarm_time 只出现在 UNION ALL 的第二个分支里（前面必须已过滤掉有历史行的告警）
  assert 'UNION ALL' in sql and 'NOT EXISTS' in sql
  assert sql.index('a.alarm_time BETWEEN') > sql.index('UNION ALL')
  _assert_sql_tokens(sql)


def test_sibling_pick():
  """兄弟证据：同群同秒另一条通报已配上的唯一 guid、且就在并列组里 → 直接采用。

  速报正文有【告警值】通常能收窄，卡片首报没有；后者继承前者结论，零模型调用。
  兄弟出现多个 guid（同秒两起事件）或 guid 不在并列组里时不继承。
  """
  cands = [
    {'event_log_id': 'g1', 'hits': 1, 'detail': 'time'},
    {'event_log_id': 'g2', 'hits': 1, 'detail': 'time'},
  ]
  assert sync._sibling_pick(cands, {'g2'}) == ('g2', 1, 'time+sibling')
  assert sync._sibling_pick(cands, {'g1', 'g2'}) is None
  assert sync._sibling_pick(cands, {'g9'}) is None
  assert sync._sibling_pick(cands, None) is None
  assert sync._sibling_pick(cands, set()) is None


def test_alarm_sql_shape():
  """展示/窗口归属也必须取**触发态**时间，与匹配侧同口径（防回退到当前态）。"""
  sql = source._ALARM_SQL
  assert 'trigger_time' in sql
  assert 'dcim_alarms_history' in sql
  assert "payload->>'status' = '1'" in sql
  assert "payload->>'timestamp'" in sql
  # 当前态 alarm_time 仍在（老告警兜底），但触发态时间必须优先由 build 读取
  assert 'a.alarm_time' in sql
  _assert_sql_tokens(sql)
  _assert_sql_tokens(source._FIRST_PUSH_SQL)
  _assert_sql_tokens(source._SIBLING_SQL)


def test_presets_all_parks():
  """只建一条 all 班次预设：不再按园区建、不再出周报。"""
  from service.scheduled_task.incident_stats import presets

  key = presets.preset_key(presets.ALL_PARKS, presets.SHIFT_PERIOD)
  assert key == 'incident__all__shift'
  assert presets.parse_key(key) == ('all', 'shift')
  assert presets._PERIODS == (presets.SHIFT_PERIOD,)
  assert presets.preset_cron(key).endswith(' 9,18 * * *')
  name = presets.preset_name(presets.ALL_PARKS, period=presets.SHIFT_PERIOD)
  assert '全部园区' in name and '班次' in name and '周报' not in name
  assert '所有已配置园区' in presets.render_prompt(key)
  # 旧的 per-park key 仍可解析（手工触发历史任务用）
  assert presets.preset_name('廊坊', period='shift') == '数据中心事件统计表 · 廊坊园区 · 班次'


def test_merge_rows_across_parks():
  """多园区行合并：按发生时间升序、序号全局重排 1..N。"""
  from service.scheduled_task.incident_stats import workflow

  rows = workflow._merge_rows([
    [{'发生时间': '2026-11-05 10:00:00', '事件名称': 'B', '序号': 1}],
    [{'发生时间': '2026-11-05 09:00:00', '事件名称': 'A', '序号': 1},
     {'发生时间': '2026-11-05 10:00:00', '事件名称': 'A', '序号': 2}],
  ])
  assert [r['事件名称'] for r in rows] == ['A', 'A', 'B']
  assert [r['序号'] for r in rows] == [1, 2, 3]


def _fake_run(key: str, status, minute: int = 0):
  from types import SimpleNamespace

  return SimpleNamespace(preset_key=key, task_id=key, status=status,
                         triggered_at=datetime(2026, 11, 6, 10, minute, tzinfo=_TZ))


def test_incident_runs_selection():
  """同期并存旧 per-park run 与新 all run 时二选一，绝不累加。"""
  from api.v1 import scheduled_tasks as api
  from models.scheduled_task import RunStatus

  key_of = {'incident__廊坊__shift': 'incident__廊坊__shift',
            'incident__平湖__shift': 'incident__平湖__shift',
            'incident__all__shift': 'incident__all__shift'}
  per_park = [_fake_run('incident__廊坊__shift', RunStatus.SUCCESS, 1),
              _fake_run('incident__平湖__shift', RunStatus.SUCCESS, 2)]
  ok_all = _fake_run('incident__all__shift', RunStatus.SUCCESS)
  picked = api._pick_incident_runs([ok_all, *per_park], key_of)
  assert list(picked) == ['all'] and picked['all'] is ok_all

  bad_all = _fake_run('incident__all__shift', RunStatus.FAILED)
  picked = api._pick_incident_runs([bad_all, *per_park], key_of)
  assert set(picked) == {'廊坊', '平湖'}

  picked = api._pick_incident_runs([bad_all], key_of)
  assert list(picked) == ['all'] and picked['all'] is bad_all

  # 同一园区重复跑：只留最新一条（runs 已按 triggered_at 倒序）
  newer = _fake_run('incident__廊坊__shift', RunStatus.SUCCESS, 3)
  picked = api._pick_incident_runs([newer, *per_park], key_of)
  assert picked['廊坊'] is newer


def test_incident_tool_partial_parks():
  """默认查全部时跳过未解析到群的园区；点名未解析的园区仍整单报错。"""
  import asyncio

  from service.scheduled_task.incident_stats import build as build_mod
  from service.scheduled_task.incident_stats import feishu as feishu_mod
  from service.scheduled_task.incident_stats import schema as schema_mod
  from tools import incident_tools as it

  orig_conf = it.settings.INCIDENT_FEISHU_PARK_CHATS
  orig = (feishu_mod.resolve_park_chats, build_mod.build_rows,
          build_mod.outside_events, schema_mod.ensure_reports_schema)

  async def fake_resolve():
    return {'廊坊': 'oc_a'}

  calls = []

  def fake_rows(start_s, end_s, park, chat_ids=None):
    calls.append((park, chat_ids))
    return []

  feishu_mod.resolve_park_chats = fake_resolve
  build_mod.build_rows = fake_rows
  build_mod.outside_events = lambda *a, **k: {
    'reports': 0, 'events': 0, 'items': [], 'truncated': False}
  schema_mod.ensure_reports_schema = lambda dsn: None
  it.settings.INCIDENT_FEISHU_PARK_CHATS = '{"廊坊":"oc_a","平湖":"oc_b"}'
  try:
    data = json.loads(asyncio.run(it.incident_report.coroutine(
      start='2026-11-05', end='2026-11-06', sync=False)))
    assert data['parks'] == ['廊坊'] and data['skipped_parks'] == ['平湖']
    assert calls == [('廊坊', ['oc_a'])]
    assert '平湖' in data['note'] and '暂不纳入' in data['note']

    named = json.loads(asyncio.run(it.incident_report.coroutine(
      start='2026-11-05', end='2026-11-06', parks='平湖', sync=False)))
    assert '未解析到 chat_id' in named['error'] and '平湖' in named['error']
  finally:
    it.settings.INCIDENT_FEISHU_PARK_CHATS = orig_conf
    (feishu_mod.resolve_park_chats, build_mod.build_rows,
     build_mod.outside_events, schema_mod.ensure_reports_schema) = orig


def test_workflow_collect_rows():
  """逐园区取数：单园区异常不影响其余；全部解析不到时由工作流判失败。"""
  import inspect

  from service.scheduled_task.incident_stats import workflow

  calls = []

  def fetch(start_s, end_s, park, chat_ids):
    calls.append((park, chat_ids))
    if park == '平湖':
      raise RuntimeError('boom')
    return [{'发生时间': '2026-11-05 10:00:00', '事件名称': 'X', '序号': 1}]

  rows, failed = workflow._collect_rows({'廊坊': 'oc_a', '平湖': 'oc_b'}, 0, 1, fetch=fetch)
  assert failed == ['平湖'] and [r['序号'] for r in rows] == [1]
  assert calls == [('廊坊', ['oc_a']), ('平湖', ['oc_b'])]
  assert workflow._collect_rows({}, 0, 1, fetch=fetch) == ([], [])
  # 一个园区群都没解析到必须判 FAILED，不能出健康空表
  assert '没有可用的园区群' in inspect.getsource(workflow.run_incident_task)


if __name__ == '__main__':
  for name, fn in sorted(globals().items()):
    if name.startswith('test_') and callable(fn):
      fn()
  print('事件统计表自检通过')
