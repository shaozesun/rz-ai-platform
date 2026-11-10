"""阈值工单报告统计的口径断言（纯 Python，可直接跑）。

跑法：`python -m service.scheduled_task.report.tests.test_threshold_compute`

**不依赖任何外部数据/网络**。合成数据刻意覆盖阈值报告独有的坑：

1. **列表接口按「计划窗口交集」筛选** → 统计前必须本地按 `scheduledStartTime`
   归属统计期，否则跨窗口的单会重复计数（样本里放了一条窗口外的诱饵）。
2. **调整意图关键词表顺序不可换**：取消 > 关闭屏蔽 > 恢复 > 提高 > 降低 > 常规修改。
3. **调整对象类型先命中先归类**（温湿度优先于供配电）。
4. **批量规模＝名称并列片段数**（顿号/逗号/分号/斜杠切分），不是设备台数。
5. **发起人**是 Python 字面量字符串；解析失败回退正则，再失败记「未标注」。
6. **按时执行＝实施结束 ≤ 计划结束**（相等算按时）；`executeEnd < executeStart` 剔除；
   未回填实施时间的不进按时率分母。
7. **超期天数只计正天数**（未到期不算超期）。
8. **确定性**：同一份输入跑两次，结果必须逐字节一致。
"""

import json

import pandas as pd

from service.scheduled_task.report.threshold_compute import compute_stats

_WINDOW = {'cur': ('2026-10-05', '2026-10-11'), 'prev': ('2026-09-28', '2026-10-04')}


def _ms(month_day: str, hour: int) -> int:
  """北京时间某天的整点 → 毫秒时间戳。"""
  ts = pd.Timestamp(f'2026-{month_day} {hour:02d}:00:00', tz='Asia/Shanghai')
  return int(ts.timestamp() * 1000)


def _fixture() -> pd.DataFrame:
  """8 行本期合成数据（1 条窗口外、1 条缺计划开始、1 条实施时间倒挂）。"""
  nan = float('nan')
  return pd.DataFrame([
    # 编号  计划开始         计划结束         创建时间         实际开始         实际结束         阶段 状态 专业 部门   项目名称            发起人
    ['T-1', _ms('10-05', 8), _ms('10-05', 10), _ms('10-04', 8),
     _ms('10-05', 8), _ms('10-05', 10), 5, 1, 1, 'A分部', 'UPS阈值取消',
     "{'name': '张三'}"],
    ['T-2', _ms('10-06', 8), _ms('10-06', 10), _ms('10-01', 8),
     _ms('10-06', 9), _ms('10-06', 12), 5, 1, 2, 'B分部', '空调温度阈值提高',
     "{'userId': 1, 'name': '李四'}"],
    ['T-3', _ms('10-07', 8), _ms('10-07', 10), _ms('09-01', 8),
     _ms('10-07', 7), _ms('10-07', 9), 2, 4, 1, 'A分部', 'UPS、电池 下电',
     "{'name': '王五'"],
    ['T-4', _ms('10-08', 8), _ms('10-08', 10), _ms('10-08', 8),
     _ms('10-08', 8), _ms('10-08', 9), 5, 1, 4, 'C分部', '门禁关闭屏蔽', ''],
    # 诱饵：计划开始在上期，接口按计划窗口交集也会返回；本地必须剔除
    ['T-5', _ms('09-20', 8), _ms('10-20', 10), _ms('09-20', 8),
     _ms('09-20', 8), _ms('09-20', 9), 5, 1, 1, 'A分部', '窗口外', "{'name': '张三'}"],
    # 缺计划开始 → 无法归属统计期
    ['T-6', nan, nan, _ms('10-01', 8), nan, nan, 5, 1, 1, 'A分部', '缺时间', ''],
    # 实施时间倒挂 → 不计有效实施、也不进按时率分母
    ['T-7', _ms('10-09', 8), _ms('10-09', 10), _ms('10-09', 8),
     _ms('10-09', 10), _ms('10-09', 9), 3, nan, 1, 'A分部', '温湿度传感器 误报',
     "{'name': '张三'}"],
    # 已排期但未实施 → 不进按时率；当前审批期
    ['T-8', _ms('10-10', 8), _ms('10-11', 10), _ms('10-10', 8),
     nan, nan, 1, 3, 2, 'B分部', '取消UPS阈值', "{'name': '赵六'}"],
  ], columns=['ticketNo', 'scheduledStartTime', 'scheduledEndTime', 'createTime',
              'executeStartTime', 'executeEndTime', 'stage', 'status', 'specialty',
              'dc', 'projectName', 'initiator'])


def _prev() -> pd.DataFrame:
  return pd.DataFrame([
    ['P-1', _ms('09-29', 8), _ms('09-29', 9), _ms('09-28', 8),
     _ms('09-29', 8), _ms('09-29', 9), 5, 1, 1, 'A分部', 'UPS阈值取消',
     "{'name': '张三'}"],
  ], columns=['ticketNo', 'scheduledStartTime', 'scheduledEndTime', 'createTime',
              'executeStartTime', 'executeEndTime', 'stage', 'status', 'specialty',
              'dc', 'projectName', 'initiator'])


def _stats():
  return compute_stats(_fixture(), _prev(), _WINDOW)


def test_scope_by_planned_start_excludes_cross_window():
  sf = _stats()['scope_filter']
  assert sf == {'raw': 8, 'kept': 6, 'out_of_window': 1, 'missing_start': 1,
                'basis': '计划实施开始时间'}, sf
  assert _stats()['total'] == 6 and _stats()['prev_total'] == 1


def test_trend_full_window_axis():
  t = _stats()['trend']
  assert len(t['labels']) == 7 and len(t['cur']) == 7, t
  assert t['cur'] == [1, 1, 1, 1, 1, 1, 0], t['cur']
  # 上一期窗口 09-28 起，P-1 在 09-29 → 第 2 个位置
  assert t['prev'] == [0, 1, 0, 0, 0, 0, 0], t['prev']


def test_intent_priority_and_unrecognized():
  it = _stats()['intent']
  rows = {r['label']: r['count'] for r in it['rows']}
  assert rows == {'取消阈值': 2, '关闭屏蔽': 1, '提高阈值': 1, '常规修改': 1,
                  '未识别': 1}, rows
  assert it['unrecognized_n'] == 1, it
  prev = {r['label']: r['prev'] for r in it['rows']}
  assert prev['取消阈值'] == 1, prev


def test_object_types_first_match_wins():
  rows = {r['label']: r['count'] for r in _stats()['object_types']}
  assert rows == {'温湿度类': 2, '供配电类': 3, '弱电智能化类': 1}, rows


def test_batch_fragments():
  bt = _stats()['batch']
  assert bt['valid'] == 6 and bt['max'] == 2, bt
  assert bt['hist'] == {'labels': ['1', '2-3', '4-9', '≥10'],
                        'counts': [5, 1, 0, 0]}, bt['hist']


def test_initiator_parse_variants():
  ini = _stats()['initiator']
  top = {r['name']: r['count'] for r in ini['top']}
  assert top == {'张三': 2, '李四': 1, '王五': 1, '赵六': 1, '未标注': 1}, top
  assert ini['unknown_n'] == 1 and ini['classes'] == 5, ini
  assert ini['top_share'] == 33.3, ini


def test_lead_bands():
  ld = _stats()['lead']
  assert ld['valid'] == 6 and ld['invalid_n'] == 0 and ld['missing_n'] == 0, ld
  assert ld['hist']['labels'][0] == '≤1天' and ld['hist']['counts'] == [4, 0, 1, 0, 0, 1], ld
  assert ld['p50'] == 720.0 and ld['readable']['p50'] == '12 小时', ld


def test_execution_on_time_boundary_and_invalid():
  ex = _stats()['execution']
  assert ex['sched_valid'] == 6, ex
  assert ex['exec_valid'] == 4 and ex['invalid_exec_n'] == 1, ex
  assert ex['missing_exec_n'] == 1, ex
  # T-1 相等算按时；T-3/T-4 提前；T-2 延迟 → 3 / 4
  assert ex['on_time_n'] == 3 and ex['on_time_base'] == 4, ex
  assert ex['on_time_rate'] == 75.0, ex
  sd = ex['start_delay']
  assert sd['late_n'] == 2 and sd['late_rate'] == 40.0, sd
  hist = {r['档位']: r['单数'] for r in sd['hist']}
  assert hist == {'提前>1天': 0, '提前1-24时': 0, '提前<1时': 1, '按时': 2,
                  '延迟<1时': 1, '延迟1-24时': 1, '延迟>1天': 0}, hist
  bs = {r['专业']: r for r in ex['by_specialty']}
  assert bs['供配电']['参与统计'] == 2 and bs['供配电']['按时率'] == 100.0, bs
  assert bs['制冷']['按时率'] == 0.0 and bs['智能化']['按时率'] == 100.0, bs


def test_flow_snapshot_and_overdue():
  fl = _stats()['threshold_flow']
  stage = {r['label']: r['count'] for r in fl['stage']}
  assert stage == {'关闭期': 3, '审批期': 1, '处理期': 1, '验收期': 1}, stage
  status = {r['label']: r['count'] for r in fl['status']}
  assert status == {'通过': 3, '实施中': 1, '审批中': 1}, status
  op = fl['open']
  assert op['n'] == 3 and op['rate'] == 50.0, op
  assert op['overdue_n'] == 2, op          # T-3 超 4 天、T-7 超 2 天；T-8 到期日＝10-11 不算超期
  assert fl['rejected_n'] == 0 and fl['pending_approval_n'] == 1, fl


def test_quality_and_kpi():
  s = _stats()
  q = s['quality']
  assert q['reject_n'] == 0 and q['reject_rate'] == 0.0, q
  assert q['close_n'] == 3 and q['close_rate'] == 50.0, q
  assert q['on_time_rate'] == 75.0, q
  assert set(s['kpi']) == {'total', 'daily_avg', 'mom', 'intent_top', 'lead_p50',
                           'on_time', 'initiator_top', 'hot_share'}, set(s['kpi'])
  assert s['kpi']['total']['value'] == '6', s['kpi']
  assert s['kpi']['intent_top']['value'] == '取消阈值', s['kpi']['intent_top']
  assert s['kpi']['on_time']['value'] == '75%', s['kpi']['on_time']
  assert s['kpi']['initiator_top']['value'] == '张三', s['kpi']['initiator_top']


def test_scorecard_dimensions_and_prev_baseline():
  """综合评分：6 个业务维度、0~100、本期与上一期同口径（雷达两条线可直接比）。"""
  sc = _stats()['scorecard']
  assert [r['维度'] for r in sc['rows']] == \
         ['规模控制', '计划前瞻', '执行达成', '闭环质量', '高危管控', '数据规范'], sc
  # 本期：单量 1→6 增幅扣到 0；提前期 P50=720 分档 85；按时率 75；
  # 闭环 0.6*50+0.4*100=70；高危（取消/关闭屏蔽 3 单）50；数据规范 83.3
  assert [r['本期'] for r in sc['rows']] == [0, 85, 75, 70, 50, 83.3], sc
  assert [r['上一期'] for r in sc['rows']] == [100, 100, 100, 100, 0, 100], sc
  assert len(sc['cur']) == len(sc['prev']) == 6, sc
  assert all(0 <= v <= 100 for v in sc['cur'] + sc['prev']), sc
  # 每个维度都要带「含义」：第 2 章分值表下方会逐条渲染，空串等于没解释
  assert all(r['含义'].strip() for r in sc['rows']), sc


def test_intent_park_and_initiator_intent_matrices():
  """交叉维度：意图×园区（热力图数据源）、发起人 Top5×意图（表格）。"""
  df = _fixture().assign(companyName=[
    '园区A', '园区B', '园区A', '园区B', '园区A', '园区A', '园区B', '园区A'])
  s = compute_stats(df, _prev(), _WINDOW)
  ip = s['intent_park']
  assert len(ip['parks']) == 2 and len(ip['intents']) >= 2, ip      # A/B 各 3 单
  assert len(ip['matrix']) == len(ip['intents']), ip
  assert all(len(row) == len(ip['parks']) for row in ip['matrix']), ip
  assert sum(sum(row) for row in ip['matrix']) == s['total'], ip
  ii = s['initiator_intent']
  assert ii['initiators'] and len(ii['intents']) == len(ip['intents']), ii
  assert sum(sum(row) for row in ii['matrix']) == s['total'], ii


def test_single_park_has_no_intent_park():
  """单园区查询不带 companyName → 无空间维度，交叉矩阵不产出。"""
  ip = _stats()['intent_park']
  assert ip == {'intents': [], 'parks': [], 'matrix': []}, ip


def test_detail_targets_deterministic_and_unique():
  nos = _stats()['detail_targets']
  assert len(nos) == 5 and len(set(nos)) == len(nos), nos
  assert 'T-3' in nos, nos                 # 批量片段最多的单会被选为代表单


def test_without_prev_period():
  s = compute_stats(_fixture(), pd.DataFrame(), _WINDOW)
  assert s['prev_total'] == 0 and s['mom']['pct'] is None, s['mom']
  assert s['trend']['prev'] == [0] * 7, s['trend']
  assert all(r['prev'] == 0 for r in s['intent']['rows']), s['intent']


def test_missing_columns_not_crash():
  df = _fixture().drop(columns=['executeStartTime', 'executeEndTime', 'initiator',
                                'specialty'])
  s = compute_stats(df, df.iloc[0:0], _WINDOW)
  assert s['total'] == 6, s['total']
  assert s['execution']['exec_valid'] == 0, s['execution']
  assert s['initiator']['top'] == [] and s['initiator']['classes'] == 0, s['initiator']
  assert s['specialty'] == [], s['specialty']


def test_deterministic():
  a = compute_stats(_fixture(), _prev(), _WINDOW)
  b = compute_stats(_fixture(), _prev(), _WINDOW)
  assert json.dumps(a, ensure_ascii=False, default=str) == \
         json.dumps(b, ensure_ascii=False, default=str)


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
