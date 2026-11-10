"""事件工单报告统计的口径断言（纯 Python，可直接跑）。

跑法：`python -m service.scheduled_task.report.tests.test_event_compute`

**不依赖任何外部数据/网络** —— 用一份**合成的小数据集**，但刻意构造成能抓住已知坑的形状：

1. **"最多的那个"不能按编码位置取**（2026-10-15 事故）：样本里 `specialty` 最多的**不是**
   编码最小的（供配电），`level` 最多的**也不是**编码最大的（4级）—— 用位置下标取就会错。
2. **`checkEndApprove` 是 Python 字面量字符串**且允许为空 → 要能解析、也要能整体缺失时降级。
3. **重复问题聚类要去首尾空白**：样本里 `'X'` 与 `' X'` 各两条，不去空白会算成 2 类 4 单，
   去空白合并成 1 类 4 单。
4. **确定性**：同一份输入跑两次，结果必须逐字节一致。
"""

import json

import pandas as pd

from service.scheduled_task.report.event_compute import compute_stats

_WINDOW = {'cur': ('2026-10-05', '2026-10-11'), 'prev': ('2026-09-28', '2026-10-04')}

# 便于阅读：北京时间某天的某个钟点 → 毫秒时间戳
def _ms(day: str, hour: int = 8) -> int:
  ts = pd.Timestamp(f'2026-10-{day} {hour:02d}:00:00', tz='Asia/Shanghai')
  return int(ts.timestamp() * 1000)


def _accept(minutes: int, timeout: bool) -> str:
  """构造 `checkEndApprove` 的**真实形态**：Python 字面量字符串（不是 JSON，True 不是 true）。"""
  return str({'approveTime': minutes * 60000, 'timeOut': timeout})


def _fixture() -> pd.DataFrame:
  """8 行合成数据。每一列的分布都是刻意设计的（见模块 docstring）。"""
  return pd.DataFrame([
    # ticketNo        occur          projectName  dc       specialty level status  checkEndApprove
    ['T-1', _ms('05', 8),  'X',         'A分部',  1, 1, 1, _accept(1, True)],
    ['T-2', _ms('05', 9),  'X',         'A分部',  2, 2, 1, _accept(2, True)],
    ['T-3', _ms('05', 10), ' X',        'A分部',  2, 2, 1, _accept(3, False)],
    ['T-4', _ms('05', 11), ' X',        'A分部',  2, 2, 1, ''],
    ['T-5', _ms('05', 12), 'Y',         'A分部',  2, 2, 2, None],
    ['T-6', _ms('06', 8),  'Z',         'B分部',  4, 3, 1, ''],
    ['T-7', _ms('06', 9),  'W',         'B分部',  4, 3, 1, None],
    ['T-8', _ms('06', 10), 'V',         'B分部',  4, 4, 1, ''],
  ], columns=['ticketNo', 'occurrenceTime', 'projectName', 'dc', 'specialty', 'level',
              'status', 'checkEndApprove']).assign(
    companyName=['A园区'] * 5 + ['B园区'] * 3,
    deleteTime=0,                      # 真实数据里恒为 0（软删标记，不是恢复时间）
    createTime=lambda d: d['occurrenceTime'],
  )


def test_distributions():
  """分布本身：按**编码**排序（保证跨期显示顺序稳定），计数与占比要对。"""
  s = compute_stats(_fixture(), pd.DataFrame(), _WINDOW)
  assert s['total'] == 8, s['total']
  # 日均按**统计期自然日**（7 天）算，不按"有单的 2 天"（否则与 7 天趋势图自相矛盾）
  assert s['daily_avg'] == 1.1, s['daily_avg']
  assert [(r['label'], r['count']) for r in s['level']] == [
    ('1级', 1), ('2级', 4), ('3级', 2), ('4级', 1)], s['level']
  assert [(r['label'], r['count']) for r in s['specialty']] == [
    ('供配电', 1), ('制冷', 4), ('智能化', 3)], s['specialty']
  assert s['park'][0]['name'] == 'A园区' and s['park'][0]['count'] == 5, s['park']


def test_prev_share_and_delta_pp():
  """跨期字段回归闸（2026-10-23 事故）：上期占比的分母是**上一期**，变化是百分点。

  解读层曾把「本期占比 40%」与「上期**计数** 27」相减写成「增加 13%」。这里锁住
  compute 产出的口径：本期 8 单/上期 6 单时，制冷占比 50% vs 66.7% → −16.7 个百分点，
  且 `delta_count` 与 `delta_pp` 是两个不同的量（不能互相冒充）。
  """
  df = _fixture()
  s = compute_stats(df, df.iloc[:6], _WINDOW)      # 上一期只取前 6 单
  chem = next(r for r in s['specialty'] if r['label'] == '制冷')
  assert chem['pct'] == 50, chem                     # 本期 4/8
  assert chem['prev_pct'] == 66.7, chem              # 上一期 4/6（不是 4/8，也不是计数 4）
  assert chem['delta_count'] == 0, chem
  assert chem['delta_pp'] == -16.7, chem
  park = {p['name']: p for p in s['park']}
  assert park['A园区']['prev_pct'] == 83.3, park['A园区']   # 5/6
  assert park['A园区']['delta_pp'] == -20.8, park['A园区']  # 62.5 − 83.3


def test_kpi_cards_pick_max_by_count():
  """**回归闸（2026-10-15 事故）**：卡片取"最多"必须按**计数**，不能按编码位置。

  样本刻意让"最多"既不落在编码首位、也不落在末位：
  specialty 最多的是 制冷(编码 2，既非首 1 也非末 4)；level 最多的是 2级(编码 2)。
  老实现用 `specialty[0]` / `level[-1]`，在这份数据上会分别答成 供配电 / 4级。
  """
  s = compute_stats(_fixture(), pd.DataFrame(), _WINDOW)
  for key, dist in (('top_level', s['level']), ('top_specialty', s['specialty'])):
    best = max(dist, key=lambda r: r['count'])
    card = s['kpi'][key]
    assert card['value'] == best['label'], f'{key} 卡片={card["value"]!r} 应为 {best["label"]!r}'
    assert str(best['count']) in card['sub'], f'{key} 卡片副标题没带计数: {card["sub"]!r}'
  # 具体值也钉死，防"以后改了样本却没人更新"
  assert s['kpi']['top_specialty']['value'] == '制冷', s['kpi']['top_specialty']
  assert s['kpi']['top_level']['value'] == '2级', s['kpi']['top_level']
  assert s['kpi']['park_top2']['value'] == '8', s['kpi']['park_top2']


def test_kpi_cards_agree_with_details():
  """卡片与明细/分布不能自相矛盾（事故的表现形式就是"卡片与正文打架"）。"""
  s = compute_stats(_fixture(), pd.DataFrame(), _WINDOW)
  k = s['kpi']
  assert k['total']['value'] == str(s['total'])
  assert k['daily_avg']['value'] == str(s['daily_avg'])
  assert k['accept_timeout']['value'] == f"{s['accept']['timeout_rate']}%"
  assert k['repeat']['value'] == str(s['quality']['repeat_classes'])
  assert k['park_top2']['value'] == str(sum(p['count'] for p in s['park'][:2]))


def test_repeat_clustering_strips_whitespace():
  """重复问题聚类**要去首尾空白**：'X' 与 ' X' 是同一条告警的两种录入。"""
  s = compute_stats(_fixture(), pd.DataFrame(), _WINDOW)
  assert s['quality']['repeat_classes'] == 1, s['quality']   # 不去空白会得 2
  assert s['quality']['repeat_n'] == 4, s['quality']         # 不去空白会得 4（但分两类）
  assert s['quality']['repeat_rate'] == 50.0, s['quality']
  assert s['repeat'][0]['name'] == 'X', s['repeat']


def test_repeat_parks_fallback_to_window_scope():
  """单园区列表不带 `companyName` → `涉及园区` 回退到 `window['scope']`（不能空白）。"""
  df = _fixture().drop(columns=['companyName'])
  s = compute_stats(df, pd.DataFrame(), {**_WINDOW, 'scope': '廊坊园区'})
  assert s['repeat'][0]['parks'] == ['廊坊园区'], s['repeat']


def test_repeat_parks_from_company_name_when_present():
  """有 `companyName`（全国口径）时按真实法人主体，不受 `scope` 影响。"""
  s = compute_stats(_fixture(), pd.DataFrame(), {**_WINDOW, 'scope': '全国各园区'})
  assert s['repeat'][0]['parks'] == ['A园区'], s['repeat']


def test_accept_duration_and_timeout():
  """验收时长解析（Python 字面量字符串）与超时率。"""
  s = compute_stats(_fixture(), pd.DataFrame(), _WINDOW)
  a = s['accept']
  assert a['valid'] == 3, a            # 3 行有 approveTime
  assert a['p50'] == 2.0, a            # 1 / 2 / 3 分钟的中位数
  assert (a['timeout_n'], a['timeout_base']) == (2, 3), a
  assert a['timeout_rate'] == 66.7, a
  assert sum(a['hist']['counts']) == 3, a['hist']
  # 分组只包含**有验收数据**的级别：样本里 3级/4级 那几行 checkEndApprove 为空，故不出现
  # （箱线图按级别分组时同理——没有数据的组不该画出一个空箱体）
  assert a['by_level'] == {'1级': [1.0], '2级': [2.0, 3.0]}, a['by_level']


def test_level_duration_matrix_bands_and_boundaries():
  """级别 × 验收时长：矩阵只含有验收数据的级别、等级段对比由平台算好、档位边界左闭右开。"""
  s = compute_stats(_fixture(), pd.DataFrame(), _WINDOW)
  ld = s['level_duration']
  assert ld['bins'] == ['≤30分', '30分-2时', '2-8时', '8-24时', '1-3天', '>3天'], ld['bins']
  # 1/2/3 分钟都在第一档；3级/4级没有验收数据 → 矩阵里整行 0
  first = ld['bins'].index('≤30分')
  assert [row[first] for row in ld['matrix']] == [1, 2, 0, 0], ld['matrix']
  assert [r['级别'] for r in ld['by_level']] == ['1级', '2级'], ld['by_level']
  assert [r['单数'] for r in ld['by_level']] == [1, 2], ld['by_level']
  # 2级的两单：1 超时 / 2 有超时标记 → 50%
  assert ld['by_level'][1]['超时率'] == 50, ld['by_level'][1]
  # 等级段：1-2 级 3 单全中；3-4 级无数据 → 不出这一段
  assert [b['等级段'] for b in ld['bands']] == ['1-2级'], ld['bands']
  band = ld['bands'][0]
  assert (band['单数'], band['超时单数'], band['超时率']) == (3, 2, 66.7), band


def test_level_duration_bins_are_data_driven():
  """档位按实测分布定（一周 165 单里 156 单都 >8 小时）：8 小时以上要再拆开，不能只剩一档。"""
  from service.scheduled_task.report.event_compute import _level_dur_bin
  cases = [(0, '≤30分'), (29.9, '≤30分'), (30, '30分-2时'), (119, '30分-2时'),
           (120, '2-8时'), (479, '2-8时'), (480, '8-24时'), (1439, '8-24时'),
           (1440, '1-3天'), (4319, '1-3天'), (4320, '>3天'), (100000, '>3天')]
  for minutes, want in cases:
    assert _level_dur_bin(minutes) == want, (minutes, _level_dur_bin(minutes), want)


def test_level_duration_degrades_without_accept():
  """没有可解析的验收时长 → 联合分析整体留空（渲染层不出表，facts 也不给空句）。"""
  df = _fixture()
  df['checkEndApprove'] = ''
  s = compute_stats(df, pd.DataFrame(), _WINDOW)
  ld = s['level_duration']
  assert ld['levels'] == [] and ld['matrix'] == [] and ld['by_level'] == [], ld


def test_reject_rate():
  s = compute_stats(_fixture(), pd.DataFrame(), _WINDOW)
  assert s['quality']['reject_n'] == 1, s['quality']
  assert s['quality']['reject_rate'] == 12.5, s['quality']


def test_deterministic():
  """同一份输入 → 结果逐字节一致（"固定口径"的底线）。"""
  a = compute_stats(_fixture(), pd.DataFrame(), _WINDOW)
  b = compute_stats(_fixture(), pd.DataFrame(), _WINDOW)
  assert json.dumps(a, sort_keys=True, ensure_ascii=False) == \
         json.dumps(b, sort_keys=True, ensure_ascii=False)


def test_empty_accept_column_degrades():
  """`checkEndApprove` 整列缺失/全空 → 标"数据不可得"，**不崩、不编**。"""
  df = _fixture()
  df['checkEndApprove'] = None
  s = compute_stats(df, pd.DataFrame(), _WINDOW)
  a = s['accept']
  assert a['available'] is False, a
  assert a['valid'] == 0 and a['timeout_rate'] is None, a
  assert sum(a['hist']['counts']) == 0, a['hist']
  assert a['by_level'] == {}, a['by_level']
  assert s['kpi']['accept_timeout']['value'] == '—', s['kpi']['accept_timeout']
  # 其余统计不受影响
  assert s['total'] == 8 and s['kpi']['top_specialty']['value'] == '制冷'


def test_trend_uses_full_window_axis():
  """趋势按**窗口完整日期轴**：10-07 之后没单也要补 0，否则两条线按各自日期集合错位。

  2026-10-15 事故：本期 6 天有单、上一期 7 天，`labels` 与 `prev` 长度不同，
  图表和「上一期同日」表格整体错位，正文据此写出了反向的大小关系。
  """
  s = compute_stats(_fixture(), pd.DataFrame(), _WINDOW)
  t = s['trend']
  assert len(t['labels']) == 7 and len(t['cur']) == 7 and len(t['prev']) == 7, t
  assert t['labels'] == ['10-05', '10-06', '10-07', '10-08', '10-09', '10-10', '10-11'], t
  assert t['cur'] == [5, 3, 0, 0, 0, 0, 0], t['cur']
  assert sum(t['cur']) == s['total'], t
  assert t['prev_days'] == ['2026-09-28', '2026-09-29', '2026-09-30', '2026-10-01',
                            '2026-10-02', '2026-10-03', '2026-10-04'], t['prev_days']


def test_without_prev_period():
  """没有上一期数据（首次跑/上期无记录）→ 环比为空、园区上期为 0，不崩。"""
  s = compute_stats(_fixture(), pd.DataFrame(), _WINDOW)
  assert s['prev_total'] == 0, s['prev_total']
  assert s['mom']['pct'] is None, s['mom']
  assert all(p['prev'] == 0 for p in s['park']), s['park']
  assert s['kpi']['mom']['value'] == '—', s['kpi']['mom']


def test_scorecard_dimensions_and_meanings():
  """综合评分：4 个业务维度、0~100、含义非空，且与本章统计口径同源。"""
  s = compute_stats(_fixture(), pd.DataFrame(), _WINDOW)
  sc = s['scorecard']
  assert [r['维度'] for r in sc['rows']] == \
         ['规模控制', '响应时效', '处置质量', '重复管控'], sc
  assert len(sc['cur']) == len(sc['prev']) == 4, sc
  assert all(0 <= v <= 100 for v in sc['cur'] + sc['prev']), sc
  assert all(r['含义'].strip() for r in sc['rows']), sc
  # 分数与统计值一致：响应时效 = 100 − 验收超时率；处置质量 = 100 − 驳回率；
  # 重复管控 = 100 − 重复占比（round 到一位，整数不带小数）
  assert sc['cur'][1] == round(100 - s['accept']['timeout_rate'], 1), sc
  assert sc['cur'][2] == round(100 - s['quality']['reject_rate'], 1), sc
  assert sc['cur'][3] == round(100 - s['quality']['repeat_rate'], 1), sc
  # 无上一期：规模控制不扣分；上一期没有验收数据 → 超时率拿不到记 0 分（不是满分）
  assert sc['rows'][0]['本期'] == 100 and sc['rows'][0]['上一期'] == 100, sc
  assert sc['rows'][1]['上一期'] == 0, sc


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
