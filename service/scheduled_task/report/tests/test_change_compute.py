"""变更工单报告统计的口径断言（纯 Python，可直接跑）。

跑法：`python -m service.scheduled_task.report.tests.test_change_compute`

**不依赖任何外部数据/网络**。合成数据刻意构造成能抓住变更报告独有的坑：

1. **列表接口的时间筛选是「计划窗口交集」，不是创建时间** → 统计前必须本地按
   **计划实施开始时间**归属统计期（样本里放了一条 9 月计划开始、窗口外结束的单当诱饵）。
2. **`modifyTypeV2` 为空时用 `modifyType` 的中文值兜底**（老数据）。
3. **按时 = 实施结束 ≤ 计划结束**（样本里 `executeEnd == scheduledEnd` 正好算按时）；
   `executeEnd < executeStart` 是脏数据，不进有效实施、也不进按时率分母。
4. **7 档完成偏差**的边界：-180 分钟进「提前 1-24时」、+90 进「延迟 1-24时」。
5. **审批/验收**是 Python 字面量字符串（`True` 不是 `true`），解析失败记不可得。
6. **确定性**：同一份输入跑两次，结果必须逐字节一致。
7. **单园区专属维度**（`dept` / 各环节耗时 / 分等级时效 / 异常率）只在**没有 `companyName`
   列**的帧上产出：全国聚合帧带该列，不能把园区内部门横向对比混进全国报告。
"""

import json

import pandas as pd

from service.scheduled_task.report.change_compute import compute_stats
from service.scheduled_task.report.common import _pct_text, _trim0

_WINDOW = {'cur': ('2026-10-05', '2026-10-11'), 'prev': ('2026-09-28', '2026-10-04')}


def _ms(month_day: str, hour: int) -> int:
  """北京时间某天的整点 → 毫秒时间戳。"""
  ts = pd.Timestamp(f'2026-{month_day} {hour:02d}:00:00', tz='Asia/Shanghai')
  return int(ts.timestamp() * 1000)


def _approve(minutes, timeout):
  return str({'approveTime': int(minutes * 60000), 'timeOut': timeout})


def _fixture() -> pd.DataFrame:
  """8 行本期合成数据（1 条窗口外、1 条缺计划开始、1 条实施时间倒挂）。"""
  nan = float('nan')
  return pd.DataFrame([
    # 编号  计划开始          计划结束          实际开始          实际结束          建单
    ['C-1', _ms('10-05', 8), _ms('10-05', 10), _ms('10-05', 8), _ms('10-05', 10),
     _ms('10-05', 7),
     1, 1, '', 5, 1, 1, '廊坊', 'A分部', '机房割接', _approve(30, False), _approve(1, False)],
    ['C-2', _ms('10-06', 8), _ms('10-06', 9), _ms('10-06', 8), _ms('10-06', 11),
     _ms('10-06', 7),
     3, 3, '', 2, 4, 2, '平湖', 'B分部', 'UPS更换', _approve(10, True), ''],
    ['C-3', _ms('10-07', 8), _ms('10-07', 10), _ms('10-07', 6), _ms('10-07', 7),
     _ms('10-07', 5),
     2, nan, '非计划型变更', 5, 1, 1, '廊坊', 'A分部', '空调检修', '', ''],
    # 诱饵：计划开始在上个月（窗口外），若直接按接口返回值统计就会误算进本期
    ['C-4', _ms('09-20', 8), _ms('10-20', 10), _ms('09-20', 8), _ms('09-20', 9),
     _ms('09-20', 7),
     1, 1, '', 5, 1, 1, '廊坊', 'A分部', '窗口外', '', ''],
    # 缺计划实施开始时间 → 无法归属统计期，单独计数
    ['C-5', nan, nan, nan, nan, nan,
     1, 1, '', 5, 1, 1, '廊坊', 'A分部', '缺时间', '', ''],
    ['C-6', _ms('10-08', 8), _ms('10-09', 8), _ms('10-08', 8), _ms('10-12', 8),
     _ms('10-08', 6),
     3, 3, '', 1, 3, 4, '惠州', 'C分部', '主干扩容',
     "{'approveTime': 120000, 'timeOut': false}", ''],
    # 实施时间倒挂（实测存在）：不算有效实施
    ['C-7', _ms('10-09', 8), _ms('10-09', 9), _ms('10-09', 10), _ms('10-09', 9),
     _ms('10-09', 8),
     1, 1, '', 5, 1, 1, '廊坊', 'A分部', '倒挂单', _approve(1, False), ''],
    # 已排期但未实施：不参与按时率
    ['C-8', _ms('10-10', 8), _ms('10-10', 10), nan, nan, nan,
     2, 2, '', 7, 9, 2, '平湖', 'B分部', '待实施', '', ''],
  ], columns=['ticketNo', 'scheduledStartTime', 'scheduledEndTime', 'executeStartTime',
              'executeEndTime', 'createTime', 'ticketType', 'modifyTypeV2', 'modifyType',
              'stage', 'status', 'specialty', 'companyName', 'dc', 'projectName',
              'checkBeginApprove', 'checkEndApprove'])


def _prev() -> pd.DataFrame:
  """上一期 2 条有效 + 1 条窗口外。"""
  return pd.DataFrame([
    ['P-1', _ms('09-29', 8), _ms('09-29', 9), 1, 1, 1, '廊坊', 'A分部'],
    ['P-2', _ms('09-30', 8), _ms('09-30', 9), 2, 2, 2, '平湖', 'B分部'],
    ['P-3', _ms('09-10', 8), _ms('09-10', 9), 1, 1, 1, '廊坊', 'A分部'],
  ], columns=['ticketNo', 'scheduledStartTime', 'scheduledEndTime', 'ticketType',
              'modifyTypeV2', 'specialty', 'companyName', 'dc'])


def _stats():
  return compute_stats(_fixture(), _prev(), _WINDOW)


def test_scope_filter_by_planned_start():
  """窗口外与缺计划开始的记录剔除并计数；趋势之和等于总量。"""
  s = _stats()
  assert s['scope_filter'] == {'raw': 8, 'kept': 6, 'out_of_window': 1,
                               'missing_start': 1, 'basis': '计划实施开始时间'}, s['scope_filter']
  assert s['total'] == 6, s['total']
  assert s['prev_total'] == 2, s['prev_total']
  assert sum(s['trend']['cur']) == s['total'], s['trend']
  assert sum(s['trend']['prev']) == s['prev_total'], s['trend']


def test_trend_uses_full_window_axis():
  """趋势按**窗口完整日期轴**对齐（2026-10-16 真实数据：本期 5 天有单、上期 7 天有单）。

  只画"有单的日期"会让两条线按各自日期集合错位，事实化的「日期 + 上一期数值」也跟着错配。
  """
  t = _stats()['trend']
  assert len(t['labels']) == 7 and len(t['cur']) == 7 and len(t['prev']) == 7, t
  assert t['labels'][0] == '10-05' and t['labels'][-1] == '10-11', t['labels']
  assert t['cur'] == [1, 1, 1, 1, 1, 1, 0], t['cur']
  # 上一期窗口 09-28~10-04：P-1 在 09-29、P-2 在 09-30 → 落在第 2、3 个位置
  assert t['prev'] == [0, 1, 1, 0, 0, 0, 0], t['prev']
  assert sum(t['cur']) == _stats()['total'], t


def test_change_type_v2_first_then_legacy():
  """`modifyTypeV2` 优先；为空的行用 `modifyType` 中文值兜底。"""
  ct = _stats()['change_type']
  rows = {r['label']: r for r in ct['rows']}
  assert rows['计划型变更']['count'] == 2, rows
  assert rows['非计划型变更']['count'] == 2, rows      # C-3 走旧字段兜底 + C-8
  assert rows['紧急变更']['count'] == 2, rows
  assert rows['计划型变更']['prev'] == 1, rows
  assert ct['emergency_n'] == 2 and ct['emergency_rate'] == 33.3, ct
  bl = {r['类型']: r for r in ct['by_level']}
  assert bl['紧急变更']['高风险'] == 2 and bl['紧急变更']['高风险占比'] == 100.0, bl
  bp = {r['园区']: r for r in ct['by_park']}
  assert bp['平湖']['紧急单数'] == 1 and bp['廊坊']['紧急单数'] == 0, bp


def test_level_and_specialty_and_park():
  s = _stats()
  assert {r['label']: r['count'] for r in s['level']} == {
    '低风险': 2, '中风险': 2, '高风险': 2}, s['level']
  assert {r['label']: r['count'] for r in s['specialty']} == {
    '供配电': 3, '制冷': 2, '智能化': 1}, s['specialty']
  assert {r['name']: r['count'] for r in s['park']} == {
    '廊坊': 3, '平湖': 2, '惠州': 1}, s['park']


def test_park_level_matrix():
  pl = _stats()['park_level']
  assert pl['levels'] == ['低风险', '中风险', '高风险'], pl
  mat = dict(zip(pl['parks'], pl['matrix']))
  assert mat['廊坊'] == [2, 1, 0], mat
  assert mat['平湖'] == [0, 1, 1], mat
  assert mat['惠州'] == [0, 0, 1], mat


def test_park_specialty_matrix():
  """园区×专业：行＝园区、列＝专业；矩阵合计等于本期单量。"""
  ps = _stats()['park_specialty']
  assert len(ps['parks']) == 3 and len(ps['specialties']) == 3, ps
  assert len(ps['matrix']) == len(ps['parks']), ps
  assert all(len(row) == len(ps['specialties']) for row in ps['matrix']), ps
  assert sum(sum(row) for row in ps['matrix']) == _stats()['total'], ps


def test_schedule_on_time_boundary_and_invalid_exec():
  """按时＝实施结束 ≤ 计划结束；倒挂单剔除、未实施不计分母。"""
  sc = _stats()['schedule']
  assert sc['sched_valid'] == 6, sc
  assert sc['exec_valid'] == 4, sc
  assert sc['invalid_exec_n'] == 1, sc
  assert sc['missing_exec_n'] == 1, sc
  assert sc['on_time_base'] == 4 and sc['on_time_n'] == 2, sc   # C-1 相等算按时、C-3 提前
  assert sc['on_time_rate'] == 50.0, sc
  assert sc['late_n'] == 2 and sc['early_n'] == 1, sc
  assert sc['planned']['p50'] == 120.0, sc['planned']
  assert sc['actual']['max'] == 5760.0, sc['actual']


def test_delay_bands():
  hist = {r['档位']: r['单数'] for r in _stats()['schedule']['delay_hist']}
  assert hist == {'提前 ≥1天': 0, '提前 1-24时': 1, '提前 <1时': 0, '按时': 1,
                  '延迟 <1时': 0, '延迟 1-24时': 1, '延迟 >1天': 1}, hist


def test_on_time_by_type_and_park():
  sc = _stats()['schedule']
  bt = {r['类型']: r for r in sc['by_type']}
  assert bt['计划型变更']['参与统计'] == 1 and bt['计划型变更']['按时率'] == 100.0, bt
  assert bt['非计划型变更']['参与统计'] == 1 and bt['非计划型变更']['按时率'] == 100.0, bt
  assert bt['紧急变更']['参与统计'] == 2 and bt['紧急变更']['按时率'] == 0.0, bt
  bp = {r['园区']: r for r in sc['by_park']}
  assert bp['廊坊']['参与统计'] == 2 and bp['廊坊']['按时率'] == 100.0, bp
  assert bp['平湖']['按时率'] == 0.0 and bp['惠州']['按时率'] == 0.0, bp
  assert [w['ticketNo'] for w in sc['worst']] == ['C-6', 'C-2'], sc['worst']


def test_flow_stage_status_and_approve():
  fl = _stats()['flow']
  stage = {r['label']: r['count'] for r in fl['stage']}
  assert stage == {'审批期': 1, '实施期': 1, '关闭期': 3, '等待期': 1}, stage
  status = {r['label']: r['count'] for r in fl['status']}
  assert status['审批中'] == 1 and status['延期中'] == 1 and status.get('驳回', 0) == 0, status
  # 审批：3 条可解析（30/10/1 分钟），1 条超时；'false' 不是合法字面量 → 不可得
  ap = fl['approve']
  assert ap['valid'] == 3 and ap['timeout_base'] == 3 and ap['timeout_n'] == 1, ap
  assert ap['timeout_rate'] == 33.3, ap
  assert ap['hist'] == {'labels': ['<1时', '1-4时', '4-24时', '1-3天', '>3天'],
                        'counts': [3, 0, 0, 0, 0]}, ap['hist']
  ac = fl['accept']
  assert ac['valid'] == 1 and ac['timeout_n'] == 0, ac
  exc = {r['异常']: r['单数'] for r in fl['exceptions']}
  assert exc == {'驳回': 0, '撤回': 0, '退回': 0, '延期中': 1}, exc
  op = fl['open']
  assert op['n'] == 3 and op['rate'] == 50.0, op
  assert op['overdue_n'] == 3, op          # 三单计划结束（10-06/10-09/10-10）都已过统计期末 10-11
  assert [r['ticketNo'] for r in op['rows']][:2] == ['C-2', 'C-6'], op['rows']


def test_flow_stage_status_joint_table():
  """阶段 × 状态联合表：按 (stage, status) 配对，合计等于总量，不能靠标签静态配对。

  「延期中」与阶段正交（样本里 C-8 是等待期 + 延期中），静态配对会把它错挂到别处；
  按单量降序、同量按码值排序，跨期稳定。
  """
  s = _stats()
  ss = s['flow']['stage_status']
  assert [(r['阶段'], r['状态']) for r in ss] == [
    ('关闭期', '通过'), ('审批期', '审批中'), ('实施期', '实施中'), ('等待期', '延期中')], ss
  assert [r['单数'] for r in ss] == [3, 1, 1, 1], ss
  assert [r['占比'] for r in ss] == [50.0, 16.7, 16.7, 16.7], ss
  assert sum(r['单数'] for r in ss) == s['total'], ss
  # 两张边际表保留（自检与异常口径在用），联合表与它们的合计一致
  assert (sum(r['count'] for r in s['flow']['stage']),
          sum(r['count'] for r in s['flow']['status'])) == (s['total'], s['total'])


def test_stage_status_absent_columns():
  """缺 stage/status 列时返回空列表（渲染层不出表，不抛错）。"""
  cur = _fixture().drop(columns=['stage', 'status'])
  assert compute_stats(cur, _prev(), _WINDOW)['flow']['stage_status'] == []


def _single_park_stats(cur=None, prev=None):
  """单园区口径：真实园区查询列表**不带 `companyName`** 列。"""
  return compute_stats(cur if cur is not None else _fixture().drop(columns=['companyName']),
                       prev if prev is not None else _prev().drop(columns=['companyName']),
                       _WINDOW)


def test_dept_block_rates_use_own_denominator():
  """部门维度只在单园区产出：单量/环比/紧急/按时/驳回，分母都是**该部门本期单量**。"""
  s = _single_park_stats()
  dept = s['dept']
  assert dept['classes'] == 3, dept
  rows = {r['name']: r for r in dept['rows']}
  assert list(rows) == ['A分部', 'B分部', 'C分部'], dept['rows']   # 按单量降序
  a = rows['A分部']
  assert (a['count'], a['prev'], a['delta'], a['pct']) == (3, 1, 2, 50), a
  assert (a['emergency_n'], a['emergency_rate']) == (0, 0), a
  # 按时率分母是「该部门内有计划+有效实施」的单（A 分部 2 单，都按时）
  assert (a['on_time_n'], a['on_time_base'], a['on_time_rate']) == (2, 2, 100), a
  assert (a['reject_n'], a['reject_rate']) == (0, 0), a
  b = rows['B分部']
  assert (b['count'], b['prev'], b['pct']) == (2, 1, 33.3), b
  assert (b['emergency_n'], b['emergency_rate']) == (1, 50), b
  assert (b['on_time_n'], b['on_time_base'], b['on_time_rate']) == (0, 1, 0), b
  c = rows['C分部']
  assert (c['count'], c['prev'], c['delta']) == (1, 0, 1), c
  assert (c['emergency_rate'], c['on_time_rate']) == (100, 0), c
  # 样本 <3 的部门不给分位（表里显示「—」），避免拿两单算 P50
  assert a['approve_p50'] is None and a['exec_p50'] is None, a
  assert sum(r['count'] for r in dept['rows']) == s['total'], dept
  # 可比字段由平台算好：上期占比分母＝上期总量（2 单）、百分点变化带符号
  assert (a['prev_pct'], a['delta_count'], a['delta_pp']) == (50, 2, 0), a
  assert (c['prev_pct'], c['delta_pp']) == (0, 16.7), c


def test_dept_excludes_blank_and_nan_names():
  """空部门 / 字面量 `nan` / `None` 不进部门表（客户端换名失败时会留下这些值）。"""
  cur = _fixture().drop(columns=['companyName'])
  cur.loc[cur['ticketNo'] == 'C-1', 'dc'] = '  '
  cur.loc[cur['ticketNo'] == 'C-2', 'dc'] = 'nan'
  s = _single_park_stats(cur)
  names = [r['name'] for r in s['dept']['rows']]
  assert names and not ({'', 'nan', 'None', 'NaN'} & set(names)), names
  assert sum(r['count'] for r in s['dept']['rows']) == 4, s['dept']   # C-1/C-2 被剔除
  assert s['total'] == 6, s['total']


def test_single_park_only_keys_absent_nationwide():
  """带 `companyName` 的全国帧**不得**产出部门与各环节键（否则全国报告混入园区内维度）。"""
  s = _stats()
  assert 'dept' not in s, s.get('dept')
  for key in ('durations', 'by_level_duration', 'exception_rates'):
    assert key not in s['flow'], (key, s['flow'].keys())
  assert 'dept' in _single_park_stats(), '单园区帧必须产出部门维度'


def test_flow_durations_rows_and_basis():
  """各环节耗时四行：审批/实施/验收口径互不相同，全流程说明不含关闭期。"""
  rows = _single_park_stats()['flow']['durations']
  assert [r['环节'] for r in rows] == ['审批', '实施', '验收', '全流程'], rows
  by = {r['环节']: r for r in rows}
  # 审批 3 条可解析（30/10/120 分钟）；'false' 不是合法字面量 → 不可得
  assert by['审批']['可统计'] == 3 and by['审批']['p50'] == 10.0, by['审批']
  assert by['审批']['p50可读'] == '10 分钟', by['审批']
  # 实施 4 单有效（C-7 倒挂剔除、C-8 未实施）：60/120/180/5760 分钟
  assert by['实施']['可统计'] == 4 and by['实施']['p50'] == 150.0, by['实施']
  assert by['实施']['p50可读'] == '2.5 小时', by['实施']   # >60 分钟换小时
  # 验收只有 C-1 一条可解析
  assert by['验收']['可统计'] == 1 and by['验收']['p50'] == 1.0, by['验收']
  # 全流程＝创建→实施完成：C-1 180 / C-2 240 / C-3 120 / C-6 5760
  assert by['全流程']['可统计'] == 4 and by['全流程']['p50'] == 210.0, by['全流程']
  # 口径列只写「创建→实施完成」：长文案会把数值列挤成「1.9 小/时」逐字断行；
  # 「无关闭时间戳 / 三段之和不等于它」已在表下 note 里，不重复
  assert by['全流程']['口径'] == '创建→实施完成', by['全流程']
  assert '倒挂' not in by['全流程']['口径'], by['全流程']


def test_flow_full_duration_excludes_inverted_create():
  """创建时间晚于实施结束（补录脏数据）→ 全流程剔除并单独计数，不当 0 也不当负值。"""
  cur = _fixture().drop(columns=['companyName'])
  cur.loc[cur['ticketNo'] == 'C-1', 'createTime'] = _ms('10-05', 11)   # 晚于 10:00 结束
  full = _single_park_stats(cur)['flow']['durations'][-1]
  assert full['可统计'] == 3 and full['p50'] == 240.0, full
  assert full['口径'] == '创建→实施完成（倒挂 1 单已剔除）', full


def test_by_level_duration_three_levels():
  """分等级时效：三档各自 P50 + 按时率；该档没有可解析值记 None（不是 0）。"""
  rows = _single_park_stats()['flow']['by_level_duration']
  by = {r['等级']: r for r in rows}
  assert list(by) == ['低风险', '中风险', '高风险'], rows
  assert all(r['单数'] == 2 for r in rows), rows
  assert (by['低风险']['审批P50分钟'], by['低风险']['实施P50分钟'],
          by['低风险']['验收P50分钟'], by['低风险']['按时率']) == (15.5, 120.0, 1.0, 100), by
  assert (by['中风险']['审批P50分钟'], by['中风险']['实施P50分钟'],
          by['中风险']['验收P50分钟'], by['中风险']['按时率']) == (None, 60.0, None, 100), by
  assert (by['高风险']['审批P50分钟'], by['高风险']['实施P50分钟'],
          by['高风险']['按时率']) == (10.0, 2970.0, 0), by


def test_exception_rates_match_status_codes():
  """异常率与 `status`/`stage` 码表一一对应，占比分母＝本期总量。"""
  fl = _single_park_stats()['flow']
  rates = {r['异常']: r for r in fl['exception_rates']}
  assert list(rates) == ['驳回', '撤回', '退回', '延期中'], fl['exception_rates']
  # 与既有异常计数同源（只多一列占比）
  assert {r['异常']: r['单数'] for r in fl['exception_rates']} == \
         {r['异常']: r['单数'] for r in fl['exceptions']}, fl
  assert [rates[k]['占比'] for k in ('驳回', '撤回', '退回')] == [0, 0, 0], rates
  assert rates['延期中']['单数'] == 1 and rates['延期中']['占比'] == 16.7, rates


def test_kpi_keys_complete():
  kpi = _stats()['kpi']
  assert set(kpi) == {'total', 'daily_avg', 'mom', 'top_level', 'emergency', 'on_time',
                      'approve_timeout', 'backlog'}, set(kpi)
  assert kpi['total']['value'] == '6', kpi['total']
  assert kpi['on_time']['value'] == '50%', kpi['on_time']
  assert kpi['emergency']['value'] == '33.3%', kpi['emergency']
  assert kpi['backlog']['value'] == '3', kpi['backlog']
  # 单位随工单类型走：变更单是「单」，不是事件/故障的「起」
  assert kpi['top_level']['sub'] == '2 单（33.3%）', kpi['top_level']


def test_detail_targets_deterministic_and_unique():
  nos = _stats()['detail_targets']
  assert nos == ['C-6', 'C-2', 'C-1', 'C-3', 'C-7'], nos
  assert len(set(nos)) == len(nos), nos


def test_trend_monthly_31_vs_30_days():
  """月报窗口 31 天 vs 上一期 30 天：上一期按位置对位，末位**没有对应日期**记 ''。

  不能补 0 当"上一期当天 0 起"：9 月没有 31 号，那一位是"不存在"，不是"零单"。
  """
  win = {'cur': ('2026-10-01', '2026-10-31'), 'prev': ('2026-09-01', '2026-09-30')}
  t = compute_stats(_fixture(), _prev(), win)['trend']
  assert len(t['labels']) == 31 and len(t['cur']) == 31, t['labels']
  assert len(t['prev']) == 31 and len(t['prev_days']) == 31, t
  assert t['prev_days'][0] == '2026-09-01' and t['prev_days'][29] == '2026-09-30', t
  assert t['prev_days'][30] == '' and t['prev'][30] == 0, t
  # P-1 在 09-29（第 29 位）、P-2 在 09-30（第 30 位）
  assert t['prev'][28] == 1 and t['prev'][29] == 1, t['prev']


def test_without_prev_period():
  """空上一期：不崩、环比占比为 None、趋势上一期为空。"""
  s = compute_stats(_fixture(), pd.DataFrame(), _WINDOW)
  assert s['prev_total'] == 0 and s['mom']['pct'] is None, s['mom']
  assert s['trend']['prev'] == [], s['trend']
  assert all(r['prev'] == 0 for r in s['change_type']['rows']), s['change_type']


def test_mom_attribution_falls_back_to_department():
  """单园区查询列表**不带 `companyName`**（2026-10-16 真实数据实测）：环比归因退回
  「部门 × 专业」，且分组键只剩一个时不得退化成标量索引（原实现直接 TypeError）。"""
  cur = _fixture().drop(columns=['companyName'])
  prev = _prev().drop(columns=['companyName'])
  s = compute_stats(cur, prev, _WINDOW)
  ex = s['extras']
  assert '部门环比变动最大' in ex and '园区环比变动最大' not in ex, ex.keys()
  rows = ex['部门环比变动最大']
  assert rows and all('部门' in r and '园区' not in r for r in rows), rows
  # 增减最大的两项：A分部（廊坊 4 单 → 0 单）与 C分部（0 → 1 单）
  assert {r['部门'] for r in rows} >= {'A分部', 'C分部'}, rows


def test_deterministic():
  a = compute_stats(_fixture(), _prev(), _WINDOW)
  b = compute_stats(_fixture(), _prev(), _WINDOW)
  assert json.dumps(a, ensure_ascii=False, default=str) == \
         json.dumps(b, ensure_ascii=False, default=str)


def test_scorecard_dimensions_and_meanings():
  """综合评分：5 个业务维度、0~100、含义非空，且与本章统计口径同源。"""
  s = _stats()
  sc = s['scorecard']
  assert [r['维度'] for r in sc['rows']] == \
         ['规模控制', '计划达成', '审批时效', '闭环质量', '紧急变更管控'], sc
  assert len(sc['cur']) == len(sc['prev']) == 5, sc
  assert all(0 <= v <= 100 for v in sc['cur'] + sc['prev']), sc
  assert all(r['含义'].strip() for r in sc['rows']), sc
  # 规模控制必须交换参数：本期 2→6 增幅扣到 0，上一期 6→2 下降给满分
  assert (sc['rows'][0]['本期'], sc['rows'][0]['上一期']) == (0, 100), sc
  q = s['quality']
  assert sc['cur'][1] == s['schedule']['on_time_rate'], sc
  assert sc['cur'][2] == round(100 - q['approve_timeout_rate'], 1), sc
  assert sc['cur'][3] == round(0.6 * (100 - q['open_rate'])
                               + 0.4 * (100 - q['reject_rate']), 1), sc
  # 紧急变更 2 单 / 6 单 = 33.3% → 66.7 分
  assert sc['cur'][4] == 66.7, sc


def test_negative_zero_is_normalized():
  """负零归一成 0（实测产物里出现过「还剩 -0 天」「提前期 -1.9 分钟（-0 小时）」）。

  `round(-0.04, 1)` 是 `-0.0`，`%g`/`+.1f` 会原样印出负号；四舍五入后等于 0 就不该带负号。
  """
  assert _trim0(-0.0) == '0'
  assert _trim0(-0.04) == '0'
  assert _trim0(-1.9 / 60) == '0'
  assert _trim0(-12.5) == '-12.5', '真实差异仍要保留负号'
  assert _pct_text(-0.04, signed=True) == '+0%'
  assert _pct_text(-0.04) == '0%'
  assert _pct_text(16.7, signed=True) == '+16.7%'
  assert _pct_text(-0.04, signed=True) != '-0%'


def main() -> int:
  """无框架跑法：逐个跑测试函数，打印结果并返回退出码。"""
  tests = [(n, f) for n, f in sorted(globals().items())
           if n.startswith('test_') and callable(f)]
  failed = []
  for name, fn in tests:
    try:
      fn()
      print(f'  OK    {name}')
    except AssertionError as e:
      failed.append(name)
      print(f'  FAIL  {name}: {e}')
    except Exception as e:                        # noqa: BLE001 — 自检脚本要报告一切异常
      failed.append(name)
      print(f'  ERROR {name}: {type(e).__name__}: {e}')
  print(f'\n{len(tests) - len(failed)}/{len(tests)} 通过'
        + (f'，失败：{failed}' if failed else ''))
  return 1 if failed else 0


if __name__ == '__main__':
  raise SystemExit(main())
