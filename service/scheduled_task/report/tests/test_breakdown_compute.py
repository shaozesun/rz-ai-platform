"""故障工单报告统计的口径断言（纯 Python，可直接跑）。

跑法：`python -m service.scheduled_task.report.tests.test_breakdown_compute`

**不依赖任何外部数据/网络** —— 合成的小数据集刻意构造成能抓住已知坑的形状：

1. **"最多的那个"不能按编码位置取**：样本里 `breakDownLevel` 最多的是 **3级**（既非最小 2 级
   也非最大 4 级）；`breakdownEquipmentType` 最多的是 **冷水机组**（不是列表首项 UPS蓄电池）。
   用位置下标取就会错（2026-10-15 事件报告踩过同名事故）。
2. **`faultHandlingTime` 是毫秒** → 要换算成分钟再分箱/分位；`>1 天` 用 1440 分钟为界
   （样本里正好放一条 1440 做边界，它**不**计入 >1 天）。
3. **重复＝设备类型 @ 发生地点**（故障名几乎不重名，按名称聚类会永远为空）。
4. **`checkEndApprove` 允许为空** → 验收时长整列不可得时要降级，不崩、不编。
5. **确定性**：同一份输入跑两次，结果必须逐字节一致。
"""

import json

import pandas as pd

from service.scheduled_task.report.breakdown_compute import compute_stats

_WINDOW = {'cur': ('2026-10-05', '2026-10-11'), 'prev': ('2026-09-28', '2026-10-04')}


def _ms(day: str, hour: int = 8) -> int:
  """北京时间某天的某个钟点 → 毫秒时间戳。"""
  ts = pd.Timestamp(f'2026-10-{day} {hour:02d}:00:00', tz='Asia/Shanghai')
  return int(ts.timestamp() * 1000)


def _mins(minutes: float) -> int:
  """分钟 → `faultHandlingTime` 的毫秒（真实字段就是这个单位）。"""
  return int(minutes * 60000)


def _fixture() -> pd.DataFrame:
  """8 行合成数据。每一列的分布都是刻意设计的（见模块 docstring）。"""
  return pd.DataFrame([
    # ticketNo occur       projectName dc      specialty level status stage checkApprove  faultTime        equip     site      companyName
    ['T-1', _ms('05', 8),  'X', 'A分部', 1, 2, 1, 5, '',            _mins(10),   'UPS蓄电池', 'M1-301', 'A园区'],
    ['T-2', _ms('05', 9),  'X', 'A分部', 2, 3, 1, 5, '',            _mins(20),   'UPS蓄电池', 'M1-301', 'A园区'],
    ['T-3', _ms('05', 10), 'X', 'A分部', 2, 3, 1, 2, '',            _mins(30),   '冷水机组',  'M1-301', 'A园区'],
    ['T-4', _ms('05', 11), 'Y', 'A分部', 2, 3, 1, 5, None,          _mins(45),   '冷水机组',  'M1-302', 'A园区'],
    ['T-5', _ms('05', 12), 'Z', 'A分部', 2, 4, 2, 2, '',            _mins(2000), '冷却塔',    'M2-101', 'B园区'],
    ['T-6', _ms('06', 8),  'W', 'B分部', 4, 4, 1, 5, '',            _mins(5),    '冷却塔',    'M2-101', 'B园区'],
    ['T-7', _ms('06', 9),  'V', 'B分部', 4, 4, 1, 5, None,          _mins(1440), 'CDU',      'M2-102', 'B园区'],
    ['T-8', _ms('06', 10), 'U', 'B分部', 4, 3, 1, 5, '',            _mins(15),   '冷水机组',  'M1-303', 'B园区'],
  ], columns=['ticketNo', 'occurrenceTime', 'projectName', 'dc', 'specialty', 'breakDownLevel',
              'status', 'stage', 'checkEndApprove', 'faultHandlingTime',
              'breakdownEquipmentType', 'occurrenceSite', 'companyName']).assign(
    deleteTime=0,                      # 真实数据里恒为 0（软删标记，不是结束时间）
  )


def test_distributions():
  """分布按**编码**排序（跨期显示顺序稳定）；故障级别字段名是 `breakDownLevel`。"""
  s = compute_stats(_fixture(), pd.DataFrame(), _WINDOW)
  assert s['total'] == 8, s['total']
  # 日均按**统计期自然日**（7 天）算，不按"有单的 2 天"（否则与 7 天趋势图自相矛盾）
  assert s['daily_avg'] == 1.1, s['daily_avg']
  assert [(r['label'], r['count']) for r in s['level']] == [
    ('2级', 1), ('3级', 4), ('4级', 3)], s['level']
  assert [(r['label'], r['count']) for r in s['specialty']] == [
    ('供配电', 1), ('制冷', 4), ('智能化', 3)], s['specialty']
  assert s['park'][0]['name'] == 'A园区' and s['park'][0]['count'] == 4, s['park']


def test_kpi_cards_pick_max_by_count():
  """**回归闸**：卡片取"最多"必须按**计数**，不能按编码/列表位置。"""
  s = compute_stats(_fixture(), pd.DataFrame(), _WINDOW)
  assert s['kpi']['top_level']['value'] == '3级', s['kpi']['top_level']
  assert s['kpi']['top_specialty']['value'] == '制冷', s['kpi']['top_specialty']
  assert s['kpi']['equipment_top']['value'] == '冷水机组', s['kpi']['equipment_top']
  assert '3' in s['kpi']['equipment_top']['sub'], s['kpi']['equipment_top']


def test_equipment_and_pareto():
  """设备类型计数、类别数、帕累托累计占比（末项＝前 N 类占总量的比例）。"""
  s = compute_stats(_fixture(), pd.DataFrame(), _WINDOW)
  e = s['equipment']
  assert e['classes'] == 4, e
  assert [(r['name'], r['count']) for r in e['rows']] == [
    ('冷水机组', 3), ('UPS蓄电池', 2), ('冷却塔', 2), ('CDU', 1)], e['rows']
  assert [r['count'] for r in e['pareto']] == [3, 2, 2, 1], e['pareto']
  # 4 类全在前 10 → 累计到 100%
  assert e['cum_pct'][-1] == 100.0, e['cum_pct']
  assert e['cum_pct'] == sorted(e['cum_pct']), e['cum_pct']


def test_fault_duration():
  """`faultHandlingTime`（毫秒）→ 分钟分位/分箱；`>1 天` 用 1440 分钟严格大于。"""
  s = compute_stats(_fixture(), pd.DataFrame(), _WINDOW)
  a = s['accept']
  assert a['valid'] == 8 and a['rows'] == 8, a
  assert a['p50'] == 25.0, a           # [5,10,15,20,30,45,1440,2000] 的中位数
  assert a['max'] == 2000.0, a
  assert sum(a['hist']['counts']) == 8, a['hist']
  # 边界：1440 分钟（正好 1 天）不计入 >1 天
  assert (a['over_1d_n'], a['over_1d_rate']) == (1, 12.5), a
  # 不足 1 小时按分钟显示（2026-10-29 起 >60 分钟才换算成小时）
  assert a['readable']['p50'] == '25 分钟', a['readable']
  # 验收时长单独解析（样本里全部为空 → 不可得），不影响处理时长
  assert a['approve_valid'] == 0 and a['approve_p50'] is None, a
  # 按级别分组只含有时长的级别
  assert set(a['by_level']) == {'2级', '3级', '4级'}, a['by_level']


def test_repeat_by_equipment_site():
  """重复 = **同类设备在同一地点**反复故障（不是按故障名称）。"""
  s = compute_stats(_fixture(), pd.DataFrame(), _WINDOW)
  assert s['quality']['repeat_classes'] == 2, s['quality']     # UPS@M1-301 / 冷却塔@M2-101
  assert s['quality']['repeat_n'] == 4, s['quality']
  assert s['quality']['repeat_rate'] == 50.0, s['quality']
  names = {r['name'] for r in s['repeat']}
  assert names == {'UPS蓄电池 @ M1-301', '冷却塔 @ M2-101'}, names


def test_quality_and_reject():
  s = compute_stats(_fixture(), pd.DataFrame(), _WINDOW)
  q = s['quality']
  assert q['reject_n'] == 1 and q['reject_rate'] == 12.5, q
  assert q['closed_n'] == 6 and q['closed_rate'] == 75.0, q


def test_deterministic():
  """同一份输入 → 结果逐字节一致（"固定口径"的底线）。"""
  a = compute_stats(_fixture(), pd.DataFrame(), _WINDOW)
  b = compute_stats(_fixture(), pd.DataFrame(), _WINDOW)
  assert json.dumps(a, sort_keys=True, ensure_ascii=False) == \
         json.dumps(b, sort_keys=True, ensure_ascii=False)


def test_empty_duration_degrades():
  """`faultHandlingTime` 整列缺失/全空 → 标"数据不可得"，**不崩、不编**。"""
  df = _fixture()
  df['faultHandlingTime'] = None
  s = compute_stats(df, pd.DataFrame(), _WINDOW)
  a = s['accept']
  assert a['available'] is False, a
  assert a['valid'] == 0 and a['p50'] is None, a
  assert sum(a['hist']['counts']) == 0, a['hist']
  assert a['by_level'] == {}, a['by_level']
  assert s['kpi']['p90_duration']['value'] == '—', s['kpi']['p90_duration']
  # 其余统计不受影响
  assert s['total'] == 8 and s['kpi']['equipment_top']['value'] == '冷水机组'


def test_without_equipment_column():
  """连设备类型列都没有 → 该维度整体降级，不崩。"""
  df = _fixture().drop(columns=['breakdownEquipmentType'])
  s = compute_stats(df, pd.DataFrame(), _WINDOW)
  assert s['equipment']['rows'] == [] and s['equipment']['classes'] == 0, s['equipment']
  assert s['kpi']['equipment_top']['value'] == '—', s['kpi']['equipment_top']
  assert s['quality']['repeat_classes'] == 0, s['quality']


def test_detail_targets_fall_back_to_equipment():
  """第 10 章的代表单：**重复聚类为空时按设备类型 Top 补足**（否则该章整章空着）。"""
  df = _fixture().drop(columns=['occurrenceSite'])      # 无地点 → 重复聚类为空
  s = compute_stats(df, pd.DataFrame(), _WINDOW)
  assert s['quality']['repeat_classes'] == 0, s['quality']
  t = s['detail_targets']
  assert t, '重复为空时必须按设备类型补足代表单'
  assert len(t) == len(set(t)), t                       # 去重
  # 4 类设备各取一张代表单（冷水机组 3 单取编号最小的 T-3，其余同类各 1-2 单）
  assert set(t) == {'T-1', 'T-3', 'T-5', 'T-7'}, t
  # 有重复时优先取重复单（UPS@M1-301 → T-1、冷却塔@M2-101 → T-5，均取编号最小的一张）
  s2 = compute_stats(_fixture(), pd.DataFrame(), _WINDOW)
  assert s2['detail_targets'][:2] == ['T-1', 'T-5'], s2['detail_targets']


def test_trend_uses_full_window_axis():
  """趋势按**窗口完整日期轴**：没单的日期补 0，`prev` 与 `labels` 等长（不再错位）。"""
  s = compute_stats(_fixture(), pd.DataFrame(), _WINDOW)
  t = s['trend']
  assert len(t['labels']) == 7 and len(t['cur']) == 7 and len(t['prev']) == 7, t
  assert t['labels'] == ['10-05', '10-06', '10-07', '10-08', '10-09', '10-10', '10-11'], t
  assert t['cur'] == [5, 3, 0, 0, 0, 0, 0], t['cur']
  assert sum(t['cur']) == s['total'], t
  assert t['prev_days'][0] == '2026-09-28' and t['prev_days'][-1] == '2026-10-04', t


def test_without_prev_period():
  """没有上一期数据 → 环比为空、园区上期为 0，不崩。"""
  s = compute_stats(_fixture(), pd.DataFrame(), _WINDOW)
  assert s['prev_total'] == 0 and s['mom']['pct'] is None, s['mom']
  assert all(p['prev'] == 0 for p in s['park']), s['park']


def test_dept_distribution():
  """故障部门分布（按 `dc`）：按 (次数降序, 名称升序) 稳定排序；缺列降级为空（第 7 章维度）。

  刻意**不用** `occurrenceSite`：那是手写自由文本、每单唯一，做不出 TOP（真实数据 56 单 → 56 个地点）。
  """
  s = compute_stats(_fixture(), pd.DataFrame(), _WINDOW)
  dept = s['dept']
  assert dept['classes'] == 2, dept
  assert [(r['name'], r['count']) for r in dept['rows']] == [
    ('A分部', 5), ('B分部', 3)], dept['rows']
  # 无上期 → prev 全 0、delta == count（发散条形的数据源）
  assert all(r['prev'] == 0 and r['delta'] == r['count'] for r in dept['rows']), dept['rows']
  assert s['kpi']['dept_top']['value'] == 'A分部', s['kpi']['dept_top']
  assert '5 单' in s['kpi']['dept_top']['sub'], s['kpi']['dept_top']
  assert '2 个部门' in s['kpi']['dept_top']['sub'], s['kpi']['dept_top']
  s2 = compute_stats(_fixture().drop(columns=['dc']), pd.DataFrame(), _WINDOW)
  assert s2['dept'] == {'rows': [], 'classes': 0}, s2['dept']
  assert s2['kpi']['dept_top']['value'] == '—', s2['kpi']['dept_top']


def test_dept_mom_delta():
  """部门环比＝本期−上期（发散条形：增为正/减为负）；本期归零的部门也要出现（键取并集）。"""
  prev = pd.DataFrame({'dc': ['A分部', 'A分部', 'B分部', 'B分部', 'B分部', 'B分部', 'C分部']})
  s = compute_stats(_fixture(), prev, _WINDOW)          # 本期 A5 / B3；上期 A2 / B4 / C1
  rows = {r['name']: r for r in s['dept']['rows']}
  assert rows['A分部']['prev'] == 2 and rows['A分部']['delta'] == 3, rows['A分部']
  assert rows['B分部']['prev'] == 4 and rows['B分部']['delta'] == -1, rows['B分部']
  assert rows['C分部']['count'] == 0 and rows['C分部']['delta'] == -1, rows['C分部']


def test_park_level_matrix():
  """各园区 × 故障级别（第 5 章横向堆叠条）：行=园区（按单量降序）、列=级别（按编码升序）。

  夹具：A园区=T1-4（级别 2/3/3/3）、B园区=T5-8（级别 4/4/4/3）→
  A=[2级1,3级3,4级0]、B=[2级0,3级1,4级3]。
  """
  s = compute_stats(_fixture(), pd.DataFrame(), _WINDOW)
  pl = s['park_level']
  assert pl['parks'] == ['A园区', 'B园区'], pl
  assert pl['levels'] == ['2级', '3级', '4级'], pl
  assert pl['matrix'] == [[1, 3, 0], [0, 1, 3]], pl['matrix']


def test_park_level_extras_has_pct():
  """各园区级别构成带**预计算占比**（图表解析要写"哪个园区高等级占比高"，解读层禁四则运算）。"""
  s = compute_stats(_fixture(), pd.DataFrame(), _WINDOW)
  rows = {r['园区']: r for r in s['extras']['各园区级别构成']}
  a, b = rows['A园区'], rows['B园区']                     # A=T1-4(2/3/3/3)、B=T5-8(4/4/4/3)
  assert a['合计'] == 4 and a['3级'] == 3 and a['3级占比'] == 75.0, a
  assert b['合计'] == 4 and b['4级占比'] == 75.0 and b['2级占比'] == 0.0, b
  for r in (a, b):                                       # 各级计数之和 = 合计；占比之和 ≈ 100
    lv_keys = [k for k in r if k.endswith('级') and not k.endswith('占比')]
    assert sum(r[k] for k in lv_keys) == r['合计'], r
    assert round(sum(r[f'{k}占比'] for k in lv_keys), 1) == 100.0, r


def test_equipment_scatter():
  """设备类型 × 处理时长：单量降序、并列按名升序；P50 为该类型处理时长的中位数。"""
  s = compute_stats(_fixture(), pd.DataFrame(), _WINDOW)
  sc = s['equipment']['scatter']
  assert [(r['name'], r['count'], r['p50_min']) for r in sc] == [
    ('冷水机组', 3, 30.0), ('UPS蓄电池', 2, 15.0), ('冷却塔', 2, 1002.5), ('CDU', 1, 1440.0)], sc
  df = _fixture()
  df['faultHandlingTime'] = None          # 处理时长全缺 → 散点为空
  assert compute_stats(df, pd.DataFrame(), _WINDOW)['equipment']['scatter'] == []


def test_park_duration_compare():
  """各园区「处理 vs 验收」P50：验收未回填时只给处理时长（None），不崩不编（第 8 章对比柱）。"""
  s = compute_stats(_fixture(), pd.DataFrame(), _WINDOW)
  cmp = s['accept']['compare']
  assert [(r['园区'], r['单数'], r['处理P50'], r['验收P50']) for r in cmp] == [
    ('A园区', 4, 25.0, None), ('B园区', 4, 727.5, None)], cmp


def test_existing_risk():
  """现存风险清单（第 10 章）：未关闭 / 长时间未处理 / 重复故障，各带代表工单。"""
  s = compute_stats(_fixture(), pd.DataFrame(), _WINDOW)
  r = s['risk']
  assert (r['open_n'], r['open_rate']) == (2, 25.0), r        # T-3、T-5 的 stage=2
  assert (r['long_n'], r['long_rate']) == (0, 0.0), r          # 无一条 > 3 天
  assert r['high_open_n'] == 1, r                              # T-5 是 4 级且未关闭
  assert [row['风险'] for row in r['rows']] == [
    '未关闭故障单', '长时间未处理（超 3 天）', '重复故障（设备类型 @ 地点）'], r['rows']
  assert r['rows'][0]['代表工单'] == ['T-3', 'T-5'], r['rows'][0]
  assert r['rows'][2]['单数'] == 4, r['rows'][2]              # UPS@M1-301 + 冷却塔@M2-101


def test_scorecard_dimensions_and_meanings():
  """综合评分：5 个业务维度、0~100、含义非空，且与本章统计口径同源。"""
  s = compute_stats(_fixture(), pd.DataFrame(), _WINDOW)
  sc = s['scorecard']
  assert [r['维度'] for r in sc['rows']] == \
         ['规模控制', '处理时效', '闭环质量', '重复故障管控', '高危故障管控'], sc
  assert len(sc['cur']) == len(sc['prev']) == 5, sc
  assert all(0 <= v <= 100 for v in sc['cur'] + sc['prev']), sc
  assert all(r['含义'].strip() for r in sc['rows']), sc
  q = s['quality']
  assert sc['cur'][1] == round(100 - q['over_1d_rate'], 1), sc
  assert sc['cur'][2] == round(0.6 * q['closed_rate'] + 0.4 * (100 - q['reject_rate']), 1), sc
  assert sc['cur'][3] == round(100 - q['repeat_rate'], 1), sc
  assert sc['cur'][4] == round(100 - s['risk']['high_open_n'] / s['total'] * 100, 1), sc


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
