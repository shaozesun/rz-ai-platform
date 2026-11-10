"""对话工具 `event_alarm_link`（事件单 ↔ DCIM 告警判定表）的自检。

跑法：`python -m service.scheduled_task.report.tests.test_event_alarm_tool`

**不连 Mongo / mgmt / PG / LLM**：判定表、事件列表、详情、判定、展示补齐、
数据集落盘全部注入桩。覆盖：

1. 缺参 / 未知园区 → 明确报错；
2. 判定表命中 → **不调 mgmt**，直接出行（inline + table_markdown + file_tag）；
3. 判定表未命中 → 拉列表 + 取详情 + 补判 + **写回判定表**；
4. >20 条走 `records` 模式（只给交互表格，不给文字表）；
5. 0 行 → `empty` + 原因文案，不产文件；
6. `link_missing=false` → 标注「未判定」，不补算、不写表；
7. 行结构与状态文案（固定 16 列，服务端渲染）。
"""

import asyncio
import contextlib
import json
from datetime import datetime, timezone

from service.scheduled_task.report import alarm_decision
from tools import event_alarm_tools as tool_mod

_TS = int(datetime(2026, 11, 2, 14, 3, tzinfo=timezone.utc).timestamp())


class _Settings:
  DCIM_ENABLED = True
  MGMT_ENABLED = True
  DCIM_PG_DSN = 'postgresql://test'


class _Store:
  def __init__(self, docs=(), *, fail_query=False, fail_save=False):
    self.docs = list(docs or [])
    self.saved = []
    self.queries = []
    self.fail_query, self.fail_save = fail_query, fail_save

  async def query(self, **kw):
    self.queries.append(kw)
    if self.fail_query:
      raise RuntimeError('mongo down')
    return list(self.docs)

  async def save(self, items):
    if self.fail_save:
      raise RuntimeError('mongo down')
    self.saved.extend(items)


def _doc(status='matched', *, ticket_no='T1', ts=_TS, scope='nationwide',
         name='市电中断', dc='A1分部', match='time',
         alarm=None, reason='') -> dict:
  doc = alarm_decision.build_decision(
    ticket_no=ticket_no, occurrence_ts=ts, scope=scope, status=status,
    name=name, dc=dc, match=match, reason=reason,
    alarm=alarm if alarm is not None else {'cluster': '廊坊A', 'event_log_id': 'G1'},
    point_sig=('廊坊A', 'd1', 'P1'), alarm_ts=ts + 10,
    alarm_snapshot={'level': '紧急', 'device': 'UPS-01', 'point': '输出电流',
                    'content': '市电中断', 'ts': ts + 10})
  return doc


def _alarm_view():
  return {
    'cluster': '廊坊A', 'event_log_id': 'G1', 'time': '2026-11-02 14:03:13',
    'ts': _TS + 10, 'level': '紧急', 'device': 'UPS-01', 'point': '输出电流',
    'content': '市电中断', 'space': '廊坊园区A1', 'value': 12.5, 'unit': 'A',
    'thresholds': {'提示': [None, 10], '一般': [None, 20],
                   '重要': [None, 30], '紧急': None},
    'breached': ['一般', '提示'], 'value_from': 'trigger',
  }


class _Env:
  """桩集合：mgmt 列表/详情、判定、展示补齐、数据集落盘。"""

  def __init__(self, *, docs=(), records=(), fields=None, verdict=None,
               detail_exc=None, store=None):
    self.store = store if store is not None else _Store(docs)
    self.records = list(records)
    self.fields = fields if fields is not None else {'事件名称': '市电中断'}
    self.verdict = verdict
    self.detail_exc = detail_exc
    self.list_calls, self.detail_calls, self.link_calls = [], [], []
    self.saved_datasets = []


@contextlib.contextmanager
def _patch(env: _Env):
  mod = tool_mod
  saved = (mod.settings, mod.get_current_user_id, mod._list_tickets,
           mod._fetch_detail, mod.alarm_decision.AlarmDecisionStore)

  async def _list(scope, window, ticket, user_id):
    env.list_calls.append({'scope': scope, 'ticket': ticket,
                           'window': window, 'user_id': user_id})
    return list(env.records)

  async def _detail(ticket_id, user_id):
    env.detail_calls.append(ticket_id)
    if env.detail_exc:
      raise env.detail_exc
    return dict(env.fields)

  async def _link(**kw):
    env.link_calls.append(kw)
    if isinstance(env.verdict, Exception):
      raise env.verdict
    out = dict(env.verdict)
    doc = out.get('decision')          # 判定文档按本次实际口径/来源重建（同真实实现）
    if doc:
      out['decision'] = alarm_decision.build_decision(
        ticket_no=kw['ticket_no'], occurrence_ts=kw['occurrence_ts'],
        scope=kw.get('scope'), status=doc['status'], name=doc.get('name') or '',
        dc=doc.get('dc') or '', match=doc.get('match') or '',
        alarm=doc.get('alarm'), point_sig=doc.get('point_sig'),
        alarm_ts=doc.get('alarm_ts'), alarm_snapshot=doc.get('alarm_snapshot'),
        reason=doc.get('reason') or '', source=kw.get('source') or 'chat')
    return out

  def _save_dataset(thread_id, capability_id, params, rows, name):
    env.saved_datasets.append({'thread_id': thread_id, 'rows': rows,
                               'capability_id': capability_id, 'name': name})
    return {'dataset_id': 'ds1'}

  def _enrich(identities, *, repo=None, repo_factory=None):
    return [_alarm_view() for _ in identities]

  async def _company_parks():
    return {'润泽科技发展有限公司': '廊坊园区'}

  saved_ds = (mod.alarm_link.enrich_identities, mod.alarm_link._company_parks,
              mod.alarm_link.link_occurrence)
  mod.settings = _Settings
  mod.get_current_user_id = lambda: 'u1'
  mod._list_tickets = _list
  mod._fetch_detail = _detail
  mod.alarm_decision.AlarmDecisionStore = lambda: env.store
  mod.alarm_link.enrich_identities = _enrich
  mod.alarm_link._company_parks = _company_parks
  mod.alarm_link.link_occurrence = _link

  import core.sandbox.datasets as ds
  saved_save = ds.save_dataset
  ds.save_dataset = _save_dataset
  try:
    yield env
  finally:
    (mod.settings, mod.get_current_user_id, mod._list_tickets,
     mod._fetch_detail, mod.alarm_decision.AlarmDecisionStore) = saved
    (mod.alarm_link.enrich_identities, mod.alarm_link._company_parks,
     mod.alarm_link.link_occurrence) = saved_ds
    ds.save_dataset = saved_save


def _call(env: _Env, **args) -> dict:
  with _patch(env):
    raw = asyncio.run(tool_mod.event_alarm_link.ainvoke(args))
  return json.loads(raw)


def _rec(no='T1', when=_TS, *, name='市电中断', dc='A1分部', tid='ID-1') -> dict:
  return {'ticketNo': no, 'occurrenceTime': int(when * 1000),
          'ticketId': tid, 'projectName': name, 'dc': dc}


def _verdict(status='matched', match='time', alarm=True) -> dict:
  doc = _doc(status=status, match=match,
             alarm={'cluster': '廊坊A', 'event_log_id': 'G1'} if alarm else None)
  return {'status': status, 'match': match, 'reason': '',
          'alarm': doc['alarm'], 'alarm_row': None, 'alarm_ts': doc['alarm_ts'],
          'alarm_snapshot': doc['alarm_snapshot'],
          'point_sig': doc['point_sig'], 'reused': False,
          'decision': doc, 'llm_calls': 1}


# ── 1. 参数校验 ──
def test_missing_params():
  out = _call(_Env())
  assert 'error' in out and 'ticket_no' in out['error'], out


def test_unknown_park():
  out = _call(_Env(), start='2026-11-02', park='火星')
  assert 'error' in out and '未知园区' in out['error'], out


# ── 2. 判定表命中：不调 mgmt ──
def test_decision_hit_skips_mgmt():
  env = _Env(docs=[_doc()])
  out = _call(env, ticket_no='T1')
  assert env.list_calls == [] and env.detail_calls == [], (env.list_calls, env.detail_calls)
  assert out['rows'] == 1 and out['reused'] == 1, out
  assert out['status_counts'] == {'matched': 1}, out
  assert out['mode'] == 'inline' and out['file_tag'].startswith('<file url='), out
  assert '| 日期 |' in out['table_markdown'], out
  assert env.saved_datasets and env.saved_datasets[0]['capability_id'] == 'event_alarm_link'


# ── 3. 未命中 → 列表 + 详情 + 补判 + 写回 ──
def test_miss_backfills_and_saves():
  env = _Env(records=[_rec()], verdict=_verdict())
  out = _call(env, ticket_no='T1', park='廊坊')
  assert len(env.list_calls) == 1 and env.list_calls[0]['scope'] == '廊坊', env.list_calls
  assert env.detail_calls == ['ID-1'], env.detail_calls
  assert env.link_calls and env.link_calls[0]['store'] is None, env.link_calls
  assert env.link_calls[0]['source'] == 'chat', env.link_calls
  assert env.store.saved and env.store.saved[0]['_id'] == alarm_decision.decision_id(
    'T1', _TS, '廊坊'), env.store.saved
  assert out['computed'] == 1 and out['llm_calls'] == 1, out
  assert out['status_counts'] == {'matched': 1}, out


def test_detail_failure_marks_row_only():
  env = _Env(records=[_rec(), _rec('T2', _TS + 60, tid='ID-2')],
             verdict=_verdict(), detail_exc=RuntimeError('mgmt 500'))
  out = _call(env, start='2026-11-02', park='廊坊')
  assert out['rows'] == 2, out
  assert out['status_counts'] == {'fetch_error': 2}, out
  assert env.link_calls == [] and env.store.saved == [], (env.link_calls, env.store.saved)
  assert out['mode'] == 'inline', out


# ── 4. >20 条只给交互表格 ──
def test_records_mode_over_20():
  docs = [_doc(ticket_no=f'T{i}', ts=_TS + i) for i in range(21)]
  out = _call(_Env(docs=docs), ticket_no='T1')
  assert out['rows'] == 21, out
  assert out['mode'] == 'records' and 'table_markdown' not in out, out
  assert out['file_tag'].endswith('name="事件单告警关联"/>'), out


# ── 5. 空结果 ──
def test_empty_window_note():
  out = _call(_Env(records=[]), start='2026-11-02')
  assert out['rows'] == 0 and out['mode'] == 'empty', out
  assert '该区间没有事件工单' in out['note'], out
  assert 'file_tag' not in out, out


def test_empty_ticket_note():
  out = _call(_Env(records=[]), ticket_no='SJ404')
  assert out['mode'] == 'empty' and '没有找到工单 SJ404' in out['note'], out


# ── 6. link_missing=false 不补算 ──
def test_link_missing_false_marks_undecided():
  env = _Env(records=[_rec()], verdict=_verdict())
  out = _call(env, ticket_no='T1', link_missing=False)
  assert env.link_calls == [] and env.store.saved == [], (env.link_calls, env.store.saved)
  assert out['status_counts'] == {'undecided': 1}, out
  assert out['computed'] == 0, out


# ── 7. 行结构 / 状态文案 ──
def test_row_columns_and_status_text():
  assert tool_mod.COLUMNS == (
    '日期', '发生时间', '工单编号', '事件名称', '部门', '关联结论', '匹配方式',
    '告警时间', '等级', '设备', '测点', '告警内容', '告警值', '四档阈值',
    '越限档', '说明')
  item = {'ticket_no': 'T1', 'ts': _TS, 'name': '市电中断', 'dc': 'A1分部',
          'view': {'status': 'matched', 'match': 'llm', 'reason': ''}}
  row = tool_mod._row(item, _alarm_view())
  assert row['关联结论'] == '已关联' and row['匹配方式'] == '模型仲裁', row
  assert row['告警值'] == '12.5 A' and row['越限档'] == '一般、提示', row
  assert '提示 高于 10' in row['四档阈值'] and '紧急 —' in row['四档阈值'], row
  assert row['工单编号'] == 'T1' and row['部门'] == 'A1分部', row

  for status, text in (('not_covered', '未覆盖（非廊坊或早于 2026-10-29）'),
                       ('no_alarm', '±2 分钟内无告警'),
                       ('ambiguous', '多候选待判定'),
                       ('unknown', '暂无法判定'),
                       ('no_time', '缺发生时间')):
    item2 = dict(item, view={'status': status, 'match': '', 'reason': ''})
    assert tool_mod._row(item2, None)['关联结论'] == text, status


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
