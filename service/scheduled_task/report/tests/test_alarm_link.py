"""事件报告「重复类目关联 DCIM 告警」的自检（纯 Python，可直接跑）。

跑法：`python -m service.scheduled_task.report.tests.test_alarm_link`

**不连库、不调模型**：仓库与仲裁器都是注入的桩。覆盖：

1. 阈值文本（告警触发条件）：`低于 10 或 高于 30` / `高于 30` / `低于 10` / `—`；
2. 通断类 `maxUse=1, maxAlarm=0` 是真上限（判据看 `maxUse`，不看 `maxAlarm`）；
3. 四档全输出；越限档可多档、按重 → 轻；
4. **每次发生各查一次**（同类 3 次发生 → 3 次点查；代表单先搜），且按**触发态时间**
   （秒级）查 `find_alarms_by_trigger_time`；
5. 代表单取 `ticketNo`，缺票号退回同类最早发生时间；
6. 单候选直接接受、不调 picker；多候选走 picker；
7. picker 返回 null / 抛异常 → 未匹配（留空不配错）；
8. 位置收敛：机房码命中时只把该空间候选交仲裁；抽不到码退回全集；
9. **全部重复类目都处理**（不设类目数上限），每个歧义类目各自一次仲裁；
10. 排序：等级降序、同级时间升序；
11. 非廊坊类目不查询、返回未匹配；
12. 窗口早于覆盖起始日 → `available=False`；
13. 关联上的告警**全部展示**（无行数上限），并由渲染层给出发生次数说明；
14. 空/异常输入不抛异常；
15. 查询传**带园区前缀**的集群代号（`廊坊A` / `廊坊B1M1` / `廊坊B1M2`）；
16. 全国口径法人主体经 `gardenName` 判园区（廊坊 → 查，佛山 → 不查）；
17. 无公司档案时按集群代号兜底，歧义码（`A18`）不猜、字段全缺判「无法确定」；
18. 显式 `park` 优先于 `companyName`；
19. 判定表：命中不查库/不调模型、未命中批量写回、读/写失败 fail-open、
    `force_refresh` 绕过读取并覆盖写、临时性失败（园区判不出 / 仲裁异常）不落表；
    代表单存下的点位签名供其余发生复用（不再查库/调模型）；
20. 阈值查询键是 `device_guid + '.' + point_guid`（`dcim_points.guid` 口径）；
21. 事件报告第 7 章：正文只留跳转句，全量清单与告警子表整体进附录。
"""

import asyncio
import contextlib
import time
from datetime import date

from service.scheduled_task.report import alarm_decision, alarm_link, render, spec

_WIN = (date(2026, 10, 26), date(2026, 11, 1))
_NOTE = alarm_link.NOTE


class _Settings:
  DCIM_ENABLED = True
  DCIM_PG_DSN = 'postgresql://test'


_SAVED = alarm_link.settings


@contextlib.contextmanager
def _gate():
  alarm_link.settings = _Settings
  try:
    yield
  finally:
    alarm_link.settings = _SAVED


def _ts(text: str) -> int:
  return int(time.mktime(time.strptime(text, '%Y-%m-%d %H:%M:%S')))


def _rec(no, name, when, *, park='廊坊园区A1数据中心', dc='A1分部') -> dict:
  row = {'ticketNo': no, 'projectName': name, 'occurrenceTime': _ts(when) * 1000,
         'dc': dc, 'level': '3级', 'specialty': '配电'}
  if park:
    row['companyName'] = park
  return row


def _alarm(guid, when, *, level=4, device='UPS-01', point='输出电流',
           content='市电中断', space='廊坊园区A1数据中心A1模组1F-PDU房',
           value=12.5, point_guid='P1', cluster='A') -> dict:
  return {'cluster': cluster, 'event_log_id': guid, 'space_guid': 's1',
          'space_name': space, 'status': '1', 'alarm_type': 'x',
          'device_guid': 'd1', 'device_name': device, 'point_guid': point_guid,
          'point_name': point, 'alarm_level': level, 'normal_range': '',
          'content': content, 'alarm_time': _ts(when), 'alarm_value': value,
          'remove_time': None, 'remove_value': None}


class _Repo:
  def __init__(self, alarms=(), trigger=None, points=None, exc=None):
    self.alarms = list(alarms)
    self.trigger = trigger or {}
    self.points = points or {}
    self.exc = exc
    self.calls = 0
    self.clusters = []
    self.trigger_queries = []
    self.trigger_pairs = []
    self.point_pairs = []
    self.closed = False

  def find_alarms_by_trigger_time(self, clusters, start_s, end_s, limit=200):
    """±2 分钟候选（**秒级**，触发态时间）。"""
    self.calls += 1
    self.clusters.append(list(clusters or []))
    self.trigger_queries.append((list(clusters or []), start_s, end_s))
    if self.exc:
      raise self.exc
    out = []
    for a in self.alarms:
      if start_s <= a['alarm_time'] <= end_s:
        row = dict(a)
        row['trigger_ts'] = row['alarm_time']
        out.append(row)
    return out[:limit]

  def find_alarms_by_ids(self, pairs):
    """按告警身份取展示字段（命中判定表后补值/阈值用）。"""
    keys = {(str(c), str(i)) for c, i in pairs or []}
    return [dict(a) for a in self.alarms
            if (str(a.get('cluster')), str(a.get('event_log_id'))) in keys]

  def find_alarm_trigger_values(self, pairs):
    self.trigger_pairs.extend(list(pairs))
    return dict(self.trigger)

  def find_point_thresholds(self, pairs):
    self.point_pairs.extend(list(pairs))
    return dict(self.points)

  def close(self):
    self.closed = True


class _Picker:
  def __init__(self, result=(1, 'ok')):
    self.result = result
    self.calls = []

  async def __call__(self, event, cands):
    self.calls.append((event, cands))
    if isinstance(self.result, Exception):
      raise self.result
    return self.result


def _stats(items) -> dict:
  return {'repeat': list(items)}


def _run(stats, recs, *, details=None, window=_WIN, park=None, repo=None,
         picker=None, company_parks=None, store=None, force_refresh=False) -> dict:
  opts = {'store': store} if store is not None else {}
  with _gate():
    return asyncio.run(alarm_link.link_repeat_alarms(
      stats, recs, details or {}, window, park=park,
      repo_factory=(lambda: repo) if repo is not None else None,
      picker=picker or _Picker(), company_parks=company_parks,
      force_refresh=force_refresh, **opts))


class _Store:
  """内存桩：与 `alarm_decision.AlarmDecisionStore` 同接口（load / save）。"""

  def __init__(self, docs=None, *, fail_load=False, fail_save=False):
    self.data = dict(docs or {})          # _id -> 判定文档
    self.loaded, self.saved = [], []
    self.fail_load, self.fail_save = fail_load, fail_save

  async def load(self, ids):
    self.loaded.extend(ids)
    if self.fail_load:
      raise RuntimeError('mongo down')
    return {k: v for k, v in self.data.items() if k in ids}

  async def save(self, items):
    if self.fail_save:
      raise RuntimeError('mongo down')
    self.saved.extend(items)
    for it in items:
      self.data[it['_id']] = it


# ── 1. 阈值文本 ──
def test_format_trigger():
  # 列里写的是「什么情况会告警」，不是正常取值范围
  assert alarm_link.format_trigger([10, 30]) == '低于 10 或 高于 30'
  assert alarm_link.format_trigger([None, 30]) == '高于 30'
  assert alarm_link.format_trigger([10, None]) == '低于 10'
  assert alarm_link.format_trigger([None, 0]) == '高于 0'
  assert alarm_link.format_trigger(None) == '—'
  assert alarm_link.format_trigger([None, None]) == '—'
  assert alarm_link.format_value(12.5, '℃') == '12.5 ℃'
  assert alarm_link.format_value(None, '℃') == '—'


# ── 2. 通断类：maxAlarm=0 是真上限 ──
def test_toggle_point_zero_is_real_bound():
  raw = {'maxUse': 1, 'maxAlarm': 0, 'minUse': 0, 'minAlarm': 0}
  assert alarm_link.bounds_of(raw) == [None, 0]
  assert alarm_link._breach(1, raw) == ('高于上限', 0.0)
  assert alarm_link._breach(0, raw) is None
  # 未配置的档位（全 0）不出边界
  assert alarm_link.bounds_of(
    {'maxUse': 0, 'maxAlarm': 0, 'minUse': 0, 'minAlarm': 0}) is None


# ── 3. 四档全输出；越限按重 → 轻 ──
def test_thresholds_and_breach_order():
  row = {
    'unit': '℃',
    'hint_alarm': {'maxUse': 1, 'maxAlarm': 10, 'minUse': 0, 'minAlarm': 0},
    'general_alarm': {'maxUse': 1, 'maxAlarm': 20, 'minUse': 0, 'minAlarm': 0},
    'important_alarm': {'maxUse': 1, 'maxAlarm': 30, 'minUse': 0, 'minAlarm': 0},
    'urgent_alarm': {'maxUse': 1, 'maxAlarm': 40, 'minUse': 0, 'minAlarm': 0},
  }
  thresholds = alarm_link._thresholds(row)
  assert list(thresholds) == ['提示', '一般', '重要', '紧急'], thresholds
  assert thresholds['提示'] == [None, 10] and thresholds['紧急'] == [None, 40]
  hints = alarm_link._breached_tiers(35, row)
  assert hints == ['重要', '一般', '提示'], hints
  assert alarm_link._breached_tiers(None, row) == []
  # 未配置的档位整档为 None（渲染 —）
  row2 = dict(row, hint_alarm={'maxUse': 0, 'maxAlarm': 0, 'minUse': 0, 'minAlarm': 0})
  assert alarm_link._thresholds(row2)['提示'] is None


# ── 3b. 阈值查询键 = device_guid + '.' + 测点代号 ──
def test_thresholds_resolved_via_composed_point_key():
  recs = [_rec('T1', '市电中断', '2026-11-02 14:03:00')]
  stats = _stats([{'name': '市电中断', 'count': 1, 'ticketNo': 'T1'}])
  points = {('A', 'd1.P1'): {
    'unit': 'A',
    'hint_alarm': {'maxUse': 1, 'maxAlarm': 10, 'minUse': 0, 'minAlarm': 0},
    'general_alarm': {'maxUse': 1, 'maxAlarm': 10, 'minUse': 0, 'minAlarm': 0},
    'important_alarm': {'maxUse': 1, 'maxAlarm': 30, 'minUse': 0, 'minAlarm': 0},
    'urgent_alarm': {'maxUse': 0, 'maxAlarm': 0, 'minUse': 0, 'minAlarm': 0},
  }}
  repo = _Repo([_alarm('G1', '2026-11-02 14:03:30')],
               trigger={('A', 'G1'): 11.0}, points=points)
  out = _run(stats, recs, repo=repo)
  alarm = out['clusters'][0]['alarms'][0]
  assert alarm['unit'] == 'A', alarm
  assert alarm['thresholds']['重要'] == [None, 30], alarm
  assert alarm['breached'] == ['一般', '提示'], alarm
  assert alarm['value'] == 11.0 and alarm['value_from'] == 'trigger', alarm
  assert ('A', 'd1.P1') in repo.point_pairs, repo.point_pairs
  assert ('A', 'G1') in repo.trigger_pairs, repo.trigger_pairs


# ── 4. 每次发生各查一次（同类 3 次发生 → 3 次点查） ──
def test_one_query_per_occurrence():
  recs = [_rec('T1', '市电中断', '2026-11-02 14:03:00'),
          _rec('T2', '市电中断', '2026-11-02 14:33:00'),
          _rec('T3', '市电中断', '2026-11-02 15:03:00'),
          _rec('T4', '空调停机', '2026-11-02 16:03:00')]
  alarms = [_alarm('G1', '2026-11-02 14:03:30'),
            _alarm('G2', '2026-11-02 14:33:30', point_guid='P2', value=13.5),
            _alarm('G3', '2026-11-02 15:03:30', point_guid='P3', value=14.5),
            _alarm('G4', '2026-11-02 16:03:30')]
  stats = _stats([{'name': '市电中断', 'count': 3, 'ticketNo': 'T1'},
                  {'name': '空调停机', 'count': 1, 'ticketNo': 'T4'}])
  repo = _Repo(alarms)
  out = _run(stats, recs, repo=repo)
  assert repo.calls == 4, repo.calls       # 3 次发生 + 1 次发生
  cluster = out['clusters'][0]
  assert cluster['total'] == 3 and cluster['matched_n'] == 3, cluster
  assert [a['time'] for a in cluster['alarms']] == [
    alarm_link._fmt_time(_ts(t)) for t in
    ('2026-11-02 14:03:30', '2026-11-02 14:33:30', '2026-11-02 15:03:30')], cluster
  assert [a['value'] for a in cluster['alarms']] == [12.5, 13.5, 14.5], cluster
  assert out['linked_n'] == 2 and out['available'] is True, out
  assert repo.closed is True


# ── 5. 代表单：缺票号退回最早发生时间 ──
def test_representative_falls_back_to_earliest():
  recs = [_rec('T2', '市电中断', '2026-11-02 14:10:00'),
          _rec('T1', '市电中断', '2026-11-02 14:03:00')]
  stats = _stats([{'name': '市电中断', 'count': 2}])
  out = _run(stats, recs, repo=_Repo([_alarm('G1', '2026-11-02 14:03:10')]))
  occ = out['clusters'][0]['occurrence']
  assert occ['ticketNo'] == 'T1', occ
  assert occ['occurred_at'] == alarm_link._fmt_time(_ts('2026-11-02 14:03:00')), occ


# ── 6/7. 单候选不仲裁；多候选仲裁；null/异常 → 未匹配 ──
def test_single_candidate_skips_picker():
  picker = _Picker()
  stats = _stats([{'name': '市电中断', 'count': 4, 'ticketNo': 'T1'}])
  recs = [_rec('T1', '市电中断', '2026-11-02 14:03:00')]
  out = _run(stats, recs, repo=_Repo([_alarm('G1', '2026-11-02 14:03:30')]),
             picker=picker)
  assert picker.calls == [], picker.calls
  cluster = out['clusters'][0]
  assert cluster['occurrence']['match'] == 'time' and len(cluster['alarms']) == 1, cluster


def _multi_setup():
  stats = _stats([{'name': '市电中断', 'count': 4, 'ticketNo': 'T1'}])
  recs = [_rec('T1', '市电中断', '2026-11-02 14:03:00')]
  alarms = [_alarm('G1', '2026-11-02 14:03:10', device='UPS-01'),
            _alarm('G2', '2026-11-02 14:04:00', device='UPS-02', point_guid='P2')]
  return stats, recs, _Repo(alarms)


def test_multi_candidate_uses_picker():
  stats, recs, repo = _multi_setup()
  picker = _Picker((2, '第二条'))
  out = _run(stats, recs, repo=repo, picker=picker)
  assert len(picker.calls) == 1, picker.calls
  cands = picker.calls[0][1]
  cluster = out['clusters'][0]
  assert cluster['occurrence']['match'] == 'llm', cluster
  assert cluster['alarms'][0]['device'] == cands[1]['device_name'], cluster
  assert out['llm_calls'] == 1


def test_signature_prior_skips_picker_for_other_occurrences():
  """代表单仲裁命中 P2 后，其余发生窗口内只要有同点位告警就直接选它。"""
  stats = _stats([{'name': '市电中断', 'count': 2, 'ticketNo': 'T1'}])
  recs = [_rec('T1', '市电中断', '2026-11-02 14:03:00'),
          _rec('T2', '市电中断', '2026-11-02 14:10:00')]
  alarms = [_alarm('G1', '2026-11-02 14:03:10', point_guid='P1', device='UPS-P1'),
            _alarm('G2', '2026-11-02 14:03:20', point_guid='P2', device='UPS-P2'),
            _alarm('G3', '2026-11-02 14:10:05', point_guid='P1', device='UPS-P1'),
            _alarm('G4', '2026-11-02 14:10:15', point_guid='P2', device='UPS-P2')]
  picker = _Picker((2, '第二条'))     # 代表单：排第 2 的 P2
  out = _run(stats, recs, repo=_Repo(alarms), picker=picker)
  assert len(picker.calls) == 1, picker.calls
  cluster = out['clusters'][0]
  assert [a['device'] for a in cluster['alarms']] == ['UPS-P2', 'UPS-P2'], cluster
  assert [a['ts'] for a in cluster['alarms']] == [
    _ts('2026-11-02 14:03:20'), _ts('2026-11-02 14:10:15')], cluster
  assert [v['match'] for v in cluster['occurrences']] == ['llm', 'point'], cluster
  assert cluster['matched_n'] == 2 and out['llm_calls'] == 1, (cluster, out)


def test_picker_null_or_error_is_unmatched():
  for result in ((None, '判不出'), RuntimeError('boom')):
    stats, recs, repo = _multi_setup()
    out = _run(stats, recs, repo=repo, picker=_Picker(result))
    cluster = out['clusters'][0]
    assert cluster['alarms'] == [] and cluster['occurrence']['match'] == '', cluster
    assert out['linked_n'] == 0, out
    assert out['available'] is True, out


# ── 8. 位置收敛 ──
def test_room_code_narrowing():
  stats = _stats([{'name': '市电中断', 'count': 2, 'ticketNo': 'T1'}])
  recs = [_rec('T1', '市电中断', '2026-11-02 14:03:00', dc='A1分部')]
  alarms = [_alarm('G1', '2026-11-02 14:03:10',
                   space='廊坊园区A1数据中心A1模组1F-PDU房'),
            _alarm('G2', '2026-11-02 14:03:40', point_guid='P2',
                   space='廊坊园区B1M2数据中心B1M2模组2F-PDU房')]
  picker = _Picker((1, 'ok'))
  out = _run(stats, recs, repo=_Repo(alarms), picker=picker)
  assert picker.calls == [], '机房码命中后只剩 1 条，不应仲裁'
  assert out['clusters'][0]['alarms'][0]['space'].endswith('PDU房')
  assert 'A1' in out['clusters'][0]['alarms'][0]['space']

  # 抽不到（或抽错）机房码 → 退回未过滤全集，仍交给模型
  recs2 = [_rec('T1', '市电中断', '2026-11-02 14:03:00', dc='ZZ9分部')]
  picker2 = _Picker((1, 'ok'))
  out2 = _run(stats, recs2, repo=_Repo(alarms), picker=picker2)
  assert len(picker2.calls) == 1 and len(picker2.calls[0][1]) == 2, picker2.calls
  assert out2['clusters'][0]['occurrence']['match'] == 'llm'


# ── 9. 每个歧义类目各自一次仲裁（无全局上限） ──
def test_each_ambiguous_category_arbitrated():
  recs = [_rec('T1', '市电中断', '2026-11-02 14:03:00'),
          _rec('T2', '空调停机', '2026-11-02 15:03:00')]
  alarms = [_alarm('G1', '2026-11-02 14:03:10'),
            _alarm('G2', '2026-11-02 14:03:50', point_guid='P2'),
            _alarm('G3', '2026-11-02 15:03:10'),
            _alarm('G4', '2026-11-02 15:03:50', point_guid='P2')]
  stats = _stats([{'name': '市电中断', 'count': 2, 'ticketNo': 'T1'},
                  {'name': '空调停机', 'count': 2, 'ticketNo': 'T2'}])
  picker = _Picker((1, 'ok'))
  out = _run(stats, recs, repo=_Repo(alarms), picker=picker)
  assert len(picker.calls) == 2, picker.calls
  assert out['llm_calls'] == 2, out
  assert all(c['alarms'] for c in out['clusters']), out


# ── 10. 排序 ──
def test_sort_alarms():
  items = [{'level': '提示', 'ts': 2}, {'level': '紧急', 'ts': 1},
           {'level': '紧急', 'ts': 0}, {'level': '一般', 'ts': 5}]
  out = alarm_link._sort_alarms(items)
  assert [(a['level'], a['ts']) for a in out] == [
    ('紧急', 0), ('紧急', 1), ('一般', 5), ('提示', 2)], out


# ── 11. 非廊坊不查询 ──
def test_non_langfang_not_queried():
  stats = _stats([{'name': '市电中断', 'count': 2, 'ticketNo': 'T1'}])
  recs = [_rec('T1', '市电中断', '2026-11-02 14:03:00', park='平湖园区B1数据中心')]
  repo = _Repo([_alarm('G1', '2026-11-02 14:03:10')])
  out = _run(stats, recs, repo=repo)
  assert repo.calls == 0, repo.calls
  cluster = out['clusters'][0]
  assert cluster['alarms'] == [] and '非廊坊' in cluster['occurrence']['reason'], cluster
  assert out['available'] is True, out


# ── 15. 查询用带园区前缀的集群代号 ──
def test_query_uses_park_prefixed_clusters():
  stats = _stats([{'name': '市电中断', 'count': 2, 'ticketNo': 'T1'}])
  recs = [_rec('T1', '市电中断', '2026-11-02 14:03:00', dc='B1M2分部')]
  repo = _Repo([_alarm('G1', '2026-11-02 14:03:10', cluster='廊坊B1M2')])
  out = _run(stats, recs, repo=repo)
  ts = _ts('2026-11-02 14:03:00')
  assert repo.clusters == [['廊坊A', '廊坊B1M1', '廊坊B1M2']], repo.clusters
  # 按**触发态时间**、**秒级**窗口查（±120s）
  assert repo.trigger_queries == [(['廊坊A', '廊坊B1M1', '廊坊B1M2'],
                                   ts - 120, ts + 120)], repo.trigger_queries
  assert out['linked_n'] == 1, out


# ── 16. 全国口径：法人主体 → gardenName 判园区 ──
def _nationwide_rec(company, when='2026-11-02 14:03:00', dc='A1分部'):
  rec = _rec('T1', '市电中断', when, park=None, dc=dc)
  rec['companyName'] = company
  return rec


def test_nationwide_langfang_entity_is_queried():
  stats = _stats([{'name': '市电中断', 'count': 2, 'ticketNo': 'T1'}])
  recs = [_nationwide_rec('润泽科技发展有限公司')]
  repo = _Repo([_alarm('G1', '2026-11-02 14:03:10')])
  out = _run(stats, recs, repo=repo,
             company_parks={'润泽科技发展有限公司': '廊坊园区'})
  assert repo.calls == 1 and out['linked_n'] == 1, (repo.calls, out)


def test_nationwide_other_entity_not_queried():
  stats = _stats([{'name': '市电中断', 'count': 2, 'ticketNo': 'T1'}])
  recs = [_nationwide_rec('广东润惠科技发展有限公司')]
  repo = _Repo([_alarm('G1', '2026-11-02 14:03:10')])
  out = _run(stats, recs, repo=repo,
             company_parks={'广东润惠科技发展有限公司': '佛山园区'})
  assert repo.calls == 0, repo.calls
  assert '非廊坊' in out['clusters'][0]['occurrence']['reason'], out


# ── 17. 无档案兜底 + 歧义码 + 字段全缺 ──
def test_company_map_needed_only_for_nationwide():
  stats = _stats([{'name': '市电中断', 'count': 2, 'ticketNo': 'T1'}])
  recs = [_nationwide_rec('润泽科技发展有限公司')]
  assert alarm_link._needs_company_map(stats['repeat'], recs, 'nationwide') is True
  assert alarm_link._needs_company_map(stats['repeat'], recs, '廊坊') is False
  assert alarm_link._needs_company_map(stats['repeat'], recs, None) is True
  # 代号已能判定（B1M2）时不必查档案
  recs2 = [_nationwide_rec('润泽科技发展有限公司', dc='B1M2分部')]
  assert alarm_link._needs_company_map(stats['repeat'], recs2, None) is False


def test_nationwide_room_code_fallback_and_ambiguous():
  stats = _stats([{'name': '市电中断', 'count': 2, 'ticketNo': 'T1'}])
  # 档案不可用，但部门带 B1M2 → 仍可判定为廊坊
  recs = [_nationwide_rec('润泽科技发展有限公司', dc='B1M2分部')]
  out = _run(stats, recs, repo=_Repo([_alarm('G1', '2026-11-02 14:03:10')]),
             company_parks={})
  assert out['linked_n'] == 1, out

  # A18 是跨园区歧义码（廊坊 / 佛山都有 A 栋），不猜
  recs2 = [_nationwide_rec('广东润惠科技发展有限公司', dc='A18分部')]
  out2 = _run(stats, recs2, repo=_Repo([_alarm('G1', '2026-11-02 14:03:10')]),
              company_parks={})
  assert out2['linked_n'] == 0 and '非廊坊' in out2['clusters'][0]['occurrence']['reason']

  # 既无法人主体、部门也没有集群代号 → 无法判断
  rec3 = _rec('T1', '市电中断', '2026-11-02 14:03:00', park=None, dc='弱电部')
  out3 = _run(stats, [rec3], repo=_Repo([_alarm('G1', '2026-11-02 14:03:10')]),
              company_parks={})
  assert out3['linked_n'] == 0 and '无法确定' in out3['clusters'][0]['occurrence']['reason']


# ── 18. 显式 park 优先 ──
def test_explicit_park_overrides_company_name():
  stats = _stats([{'name': '市电中断', 'count': 2, 'ticketNo': 'T1'}])
  recs = [_nationwide_rec('润泽科技发展有限公司')]
  repo = _Repo([_alarm('G1', '2026-11-02 14:03:10')])
  out = _run(stats, recs, park='廊坊', repo=repo)   # company_parks 空也不影响
  assert repo.calls == 1 and out['linked_n'] == 1, (repo.calls, out)


# ── 12. 覆盖起始日门控 ──
def test_window_before_coverage():
  stats = _stats([{'name': '市电中断', 'count': 2, 'ticketNo': 'T1'}])
  recs = [_rec('T1', '市电中断', '2026-09-02 14:03:00')]
  out = _run(stats, recs, window=(date(2026, 8, 31), date(2026, 9, 30)),
             repo=_Repo())
  assert out['available'] is False, out
  assert '2026-10-29' in out['reason'], out['reason']


# ── 13. 全部类目都查询（不设类目数上限） ──
def test_all_clusters_linked():
  names = [f'问题{i}' for i in range(7)]
  recs, alarms = [], []
  for i, name in enumerate(names):
    when = f'2026-11-02 {14 + i // 6}:{10 + i % 6 * 5:02d}:00'
    recs.append(_rec(f'T{i}', name, when))
    alarms.append(_alarm(f'G{i}', when))
  stats = _stats([{'name': n, 'count': 2, 'ticketNo': f'T{i}'}
                  for i, n in enumerate(names)])
  repo = _Repo(alarms)
  out = _run(stats, recs, repo=repo)
  assert repo.calls == len(names), repo.calls
  assert [c['name'] for c in out['clusters']] == names, out['clusters']
  assert out['linked_n'] == len(names), out


def test_render_all_alarms_and_occurrence_note():
  alarm = {'time': '2026-11-02 14:03', 'level': '紧急', 'device': 'UPS-01',
           'point': '输出电流', 'content': '市电中断', 'value': 12.5, 'unit': '℃',
           'thresholds': {'提示': [None, 10], '一般': [None, 20],
                          '重要': [None, 30], '紧急': None},
           'breached': ['重要'], 'value_from': 'trigger'}
  link = {'available': True, 'note': _NOTE, 'linked_n': 1, 'clusters': [
    {'name': '市电中断', 'count': 4, 'total': 4, 'matched_n': 2,
     'occurrence': {}, 'alarms': [alarm] * 5}]}
  html = render._alarm_link_block(link)
  assert '另有' not in html, html
  assert html.count('<tr>') == 6, html          # 表头 + 全部 5 行，不截断
  assert '高于 10' in html and '高于 30' in html, html
  assert '本期该类共 4 次发生，其中 2 次关联到 DCIM 告警' in html, html
  assert html.count('<h3>') == 1, html

  # 全部关联到时写「均关联到」
  full = {'available': True, 'note': _NOTE, 'clusters': [
    {'name': '市电中断', 'count': 2, 'total': 2, 'matched_n': 2,
     'occurrence': {}, 'alarms': [alarm]}]}
  assert '本期该类共 2 次发生，均关联到 DCIM 告警' in render._alarm_link_block(full)

  # 重复报单：6 条工单去重后 5 次发生 → 说明沿用标题的次数口径，不出现第二个数字
  dup = {'available': True, 'note': _NOTE, 'clusters': [
    {'name': '市电中断', 'count': 6, 'total': 5, 'matched_n': 2,
     'occurrence': {}, 'alarms': [alarm]}]}
  html = render._alarm_link_block(dup)
  assert '本期该类共 6 次发生，其中 2 次关联到 DCIM 告警' in html, html
  assert '去重后' not in html, html

  empty = {'available': True, 'note': _NOTE, 'clusters': [
    {'name': 'x', 'count': 1, 'occurrence': {}, 'alarms': []}]}
  assert '未关联到 DCIM 告警' in render._alarm_link_block(empty)


# ── 19. 判定表 ──
def _did(ts_text='2026-11-02 14:03:00', no='T1', scope='nationwide'):
  return alarm_decision.decision_id(no, _ts(ts_text), scope)


def test_decision_hit_skips_query_and_picker():
  stats = _stats([{'name': '市电中断', 'count': 5, 'ticketNo': 'T1'}])
  recs = [_rec('T1', '市电中断', '2026-11-02 14:03:00')]
  store = _Store()
  first = _run(stats, recs, repo=_Repo([_alarm('G1', '2026-11-02 14:03:10')]),
               store=store)
  assert first['linked_n'] == 1, first
  # 第二轮：判定表命中 → 不查时间窗、不调仲裁
  repo = _Repo(exc=RuntimeError('命中判定表不应查库'))
  picker = _Picker(RuntimeError('命中判定表不应仲裁'))
  out = _run(stats, recs, repo=repo, picker=picker, store=store)
  assert repo.calls == 0 and repo.trigger_queries == [], repo.trigger_queries
  assert picker.calls == [], picker.calls
  assert out['cache_hits'] == 1 and out['linked_n'] == 1, out
  assert out['clusters'][0]['count'] == 5, out['clusters'][0]


def test_decision_miss_writes_back():
  stats = _stats([{'name': '市电中断', 'count': 2, 'ticketNo': 'T1'}])
  recs = [_rec('T1', '市电中断', '2026-11-02 14:03:00', dc='A1分部')]
  store = _Store()
  out = _run(stats, recs, repo=_Repo([_alarm('G1', '2026-11-02 14:03:10')]),
             store=store)
  assert len(store.saved) == 1, store.saved
  doc = store.saved[0]
  assert doc['_id'] == _did(), doc
  assert doc['ticket_no'] == 'T1' and doc['scope'] == 'nationwide', doc
  assert doc['status'] == 'matched' and doc['match'] == 'time', doc
  assert doc['alarm'] == {'cluster': 'A', 'event_log_id': 'G1'}, doc
  assert doc['point_sig'] == ['A', 'd1', 'P1'], doc
  assert doc['alarm_ts'] == _ts('2026-11-02 14:03:10'), doc
  assert doc['alarm_snapshot']['device'] == 'UPS-01', doc
  assert doc['rule_version'] == alarm_decision.RULE_VERSION, doc
  assert out['clusters'][0]['alarms'], out


def test_decision_scope_is_part_of_key():
  """同单同时间、不同口径的判定互不串用（W45 事故的回归闸）。"""
  ts = _ts('2026-11-02 14:03:00')
  assert _did(scope='nationwide') != alarm_decision.decision_id('T1', ts, '廊坊')
  stats = _stats([{'name': '市电中断', 'count': 2, 'ticketNo': 'T1'}])
  recs = [_nationwide_rec('广东润惠科技发展有限公司')]
  store = _Store()
  _run(stats, recs, repo=_Repo(), store=store,
       company_parks={'广东润惠科技发展有限公司': '佛山园区'})
  assert store.saved[0]['scope'] == 'nationwide', store.saved
  assert store.saved[0]['status'] == 'not_covered', store.saved
  # 换园区口径 → 新键、不命中全国口径的「非廊坊」结论
  local = _Store()
  out = _run(stats, recs, repo=_Repo([_alarm('G1', '2026-11-02 14:03:10')]),
             park='廊坊', store=local)
  assert out['cache_hits'] == 0 and out['linked_n'] == 1, out
  assert local.loaded[0] == alarm_decision.decision_id('T1', ts, '廊坊'), local.loaded


def test_decision_store_fail_open():
  stats = _stats([{'name': '市电中断', 'count': 2, 'ticketNo': 'T1'}])
  recs = [_rec('T1', '市电中断', '2026-11-02 14:03:00')]
  alarms = [_alarm('G1', '2026-11-02 14:03:10')]

  load_fail = _Store(fail_load=True)
  repo = _Repo(alarms)
  out = _run(stats, recs, repo=repo, store=load_fail)
  assert repo.calls == 1 and out['linked_n'] == 1, (repo.calls, out)
  assert load_fail.saved, load_fail.saved

  save_fail = _Store(fail_save=True)
  repo2 = _Repo(alarms)
  out2 = _run(stats, recs, repo=repo2, store=save_fail)
  assert repo2.calls == 1 and out2['linked_n'] == 1, (repo2.calls, out2)
  assert save_fail.saved == [], save_fail.saved


def test_decision_force_refresh_overrides():
  stats = _stats([{'name': '市电中断', 'count': 2, 'ticketNo': 'T1'}])
  recs = [_rec('T1', '市电中断', '2026-11-02 14:03:00')]
  stale = alarm_decision.build_decision(
    ticket_no='T1', occurrence_ts=_ts('2026-11-02 14:03:00'), scope='nationwide',
    status='matched', name='市电中断', match='llm',
    alarm={'cluster': 'A', 'event_log_id': 'G-OLD'})
  store = _Store({stale['_id']: stale})
  repo = _Repo([_alarm('G1', '2026-11-02 14:03:10')])
  out = _run(stats, recs, repo=repo, store=store, force_refresh=True)
  assert store.loaded == [], store.loaded
  assert repo.calls == 1 and out['cache_hits'] == 0, (repo.calls, out)
  assert out['clusters'][0]['alarms'][0]['device'] == 'UPS-01', out['clusters'][0]
  assert store.saved and store.saved[0]['_id'] == stale['_id'], store.saved
  assert store.saved[0]['alarm']['event_log_id'] == 'G1', store.saved


def test_transient_failure_not_stored():
  stats, recs, repo = _multi_setup()
  store = _Store()
  out = _run(stats, recs, repo=repo, picker=_Picker(RuntimeError('boom')),
             store=store)
  assert out['linked_n'] == 0 and store.saved == [], (out, store.saved)

  no_park = _stats([{'name': '市电中断', 'count': 2, 'ticketNo': 'T1'}])
  recs2 = [_rec('T1', '市电中断', '2026-11-02 14:03:00', park=None, dc='弱电部')]
  store2 = _Store()
  out2 = _run(no_park, recs2, repo=_Repo([_alarm('G1', '2026-11-02 14:03:10')]),
              store=store2, company_parks={})
  assert '无法确定' in out2['clusters'][0]['occurrence']['reason'], out2
  assert store2.saved == [], store2.saved


def test_company_registry_unavailable_not_stored():
  """档案不可用（`_company_parks` → None）时判不出园区，不能写死「非廊坊」。"""
  stats = _stats([{'name': '市电中断', 'count': 2, 'ticketNo': 'T1'}])
  recs = [_nationwide_rec('润泽科技发展有限公司', dc='弱电部')]
  store = _Store()
  saved = alarm_link._company_parks

  async def _none():
    return None

  alarm_link._company_parks = _none
  try:
    with _gate():
      out = asyncio.run(alarm_link.link_repeat_alarms(
        stats, recs, {}, _WIN, repo_factory=lambda: _Repo(
          [_alarm('G1', '2026-11-02 14:03:10')]),
        picker=_Picker(), store=store))
  finally:
    alarm_link._company_parks = saved
  assert '无法确定' in out['clusters'][0]['occurrence']['reason'], out
  assert out['linked_n'] == 0 and store.saved == [], (out, store.saved)


def test_stored_point_signature_reused_by_other_occurrences():
  """代表单判定里存的点位签名，供其余发生确定性复用（不查库样本、不调模型）。"""
  stats = _stats([{'name': '市电中断', 'count': 2, 'ticketNo': 'T1'}])
  recs = [_rec('T1', '市电中断', '2026-11-02 14:03:00'),
          _rec('T2', '市电中断', '2026-11-02 14:10:00')]
  first = alarm_decision.build_decision(
    ticket_no='T1', occurrence_ts=_ts('2026-11-02 14:03:00'),
    scope='nationwide', status='matched', name='市电中断', match='point',
    alarm={'cluster': 'A', 'event_log_id': 'G1'},
    point_sig=('A', 'd1', 'P1'), alarm_ts=_ts('2026-11-02 14:03:10'))
  store = _Store({first['_id']: first})
  alarms = [_alarm('G1', '2026-11-02 14:03:10', point_guid='P1', device='UPS-P1'),
            _alarm('G3', '2026-11-02 14:10:05', point_guid='P1', device='UPS-P1'),
            _alarm('G4', '2026-11-02 14:10:15', point_guid='P2', device='UPS-P2')]
  picker = _Picker(RuntimeError('签名命中不应仲裁'))
  out = _run(stats, recs, repo=_Repo(alarms), picker=picker, store=store)
  assert picker.calls == [], picker.calls
  assert out['cache_hits'] == 1, out
  cluster = out['clusters'][0]
  assert [v['match'] for v in cluster['occurrences']] == ['point', 'point'], cluster
  assert [a['time'] for a in cluster['alarms']] == [
    alarm_link._fmt_time(_ts('2026-11-02 14:03:10')),
    alarm_link._fmt_time(_ts('2026-11-02 14:10:05'))], cluster
  assert store.saved and store.saved[0]['ticket_no'] == 'T2', store.saved


# ── 20. 事件报告第 7 章：正文留跳转，清单与子表进附录 ──
def _repeat_ctx(link_spec):
  rep = [{'name': '市电中断', 'count': 4, 'parks': ['廊坊园区'], 'dcs': ['A1分部']},
         {'name': '空调停机', 'count': 3, 'parks': ['廊坊园区'], 'dcs': ['A1分部']}]
  alarm = {'time': '2026-11-02 14:03', 'level': '紧急', 'device': 'UPS-01',
           'point': '输出电流', 'content': '市电中断', 'value': 12.5, 'unit': 'A',
           'thresholds': None, 'breached': [], 'value_from': 'trigger'}
  link = {'available': True, 'note': _NOTE, 'linked_n': 1, 'clusters': [
    {'name': '市电中断', 'count': 4, 'occurrence': {}, 'alarms': [alarm]}]}
  return render._Ctx({'repeat': rep}, {'alarm_links': link}, {},
                     link_spec, set(), {})


def test_repeat_section_links_to_appendix():
  ctx = _repeat_ctx(spec.EVENT_SPEC)
  html = render._r_repeat(ctx, 7)
  assert 'href="#sec-repeat-appendix"' in html, html
  assert '市电中断' not in html, html          # 明细已移出正文
  assert '<table' not in html, html
  assert len(ctx.appendix) == 1, ctx.appendix
  anchor, title, block = ctx.appendix[0]
  assert anchor == 'sec-repeat-appendix', anchor
  assert '重复问题与关联告警' in title, title
  for text in ('市电中断', '空调停机', 'UPS-01', '输出电流'):
    assert text in block, (text, block)


def test_repeat_section_inline_when_not_event():
  import dataclasses

  ctx = _repeat_ctx(dataclasses.replace(spec.EVENT_SPEC, repeat_appendix=False))
  html = render._r_repeat(ctx, 7)
  assert '市电中断' in html and '<table' in html, html
  assert 'sec-repeat-appendix' not in html, html
  assert ctx.appendix == [], ctx.appendix


# ── 14. 空 / 异常输入 ──
def test_empty_and_error_inputs():
  out = _run({}, [], repo=_Repo())
  assert out['available'] is True and out['clusters'] == [], out

  stats = _stats([{'name': '市电中断', 'count': 2, 'ticketNo': 'T1'}])
  no_time = {'ticketNo': 'T1', 'projectName': '市电中断',
             'companyName': '廊坊园区A1数据中心', 'dc': 'A1分部'}
  out2 = _run(stats, [no_time], repo=_Repo([_alarm('G1', '2026-11-02 14:03:10')]))
  assert out2['clusters'][0]['alarms'] == [], out2
  assert '缺少发生时间' in out2['clusters'][0]['occurrence']['reason'], out2

  out3 = _run(stats, [_rec('T1', '市电中断', '2026-11-02 14:03:00')],
              repo=_Repo(exc=RuntimeError('db down')))
  assert out3['available'] is False and 'db down' in out3['reason'], out3

  with _gate():
    alarm_link.settings = type('S', (), {'DCIM_ENABLED': False, 'DCIM_PG_DSN': ''})
    try:
      out4 = asyncio.run(alarm_link.link_repeat_alarms(stats, [], {}, _WIN))
    finally:
      alarm_link.settings = _SAVED
  assert out4['available'] is False and '未启用' in out4['reason'], out4


def test_parse_pick():
  assert alarm_link.parse_pick('{"pick": 2}', 3) == 2
  assert alarm_link.parse_pick('{"pick": null}', 3) is None
  assert alarm_link.parse_pick('```json\n{"pick": 3}\n```', 3) == 3
  assert alarm_link.parse_pick('{"pick": 5}', 3) is None
  assert alarm_link.parse_pick('not json', 3) is None
  assert alarm_link.parse_pick('{"pick": true}', 3) is None


if __name__ == '__main__':
  tests = [(name, fn) for name, fn in sorted(globals().items())
           if name.startswith('test_') and callable(fn)]
  failed = 0
  for name, fn in tests:
    try:
      fn()
      print(f'  OK    {name}')
    except AssertionError as exc:
      failed += 1
      print(f'  FAIL  {name}: {exc}')
  print(f'\n{len(tests) - failed}/{len(tests)} 通过')
  raise SystemExit(1 if failed else 0)
