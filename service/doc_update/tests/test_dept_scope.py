"""部门口径判定自检（纯 assert、不连库）：按期提交 / 原地更新两种判据。

跑法：`python -m service.doc_update.tests.test_dept_scope`
"""

from datetime import datetime, timedelta, timezone

from service.doc_update.fetch import merge_snapshot
from service.doc_update.verdict import (
  STATUS_BASELINE,
  STATUS_FAILED,
  STATUS_MISSING,
  STATUS_STALE,
  STATUS_UPDATED,
  evaluate,
  summary_text,
)

NOW = datetime(2026, 11, 3, tzinfo=timezone.utc)
EXPECTED = '2026-10'


def _period_file(fid, period, path='2026/9月'):
  return {
    'fid': fid, 'file_name': f'{fid}.pdf', 'path': path, 'period': period,
    'size': 100, 'download_size': 0, 'sha256': '', 'error': '', 'warn': '',
    'last_changed_at': '',
  }


def _flat_file(fid, sha, days_ago=5):
  return {
    'fid': fid, 'file_name': f'{fid}.pdf', 'path': '线上更新', 'period': '',
    'size': 100, 'download_size': 100, 'sha256': sha,
    'error': '' if sha else 'boom', 'warn': '',
    'last_changed_at': NOW - timedelta(days=days_ago) if sha else '',
  }


def _item(seq, number, name, layout, files, latest='', error='', actual_dirs=None):
  return {
    'seq': seq, 'number': number, 'name': name, 'cadence': '月度', 'note': '',
    'layout': layout, 'expected_period': EXPECTED, 'latest_period': latest,
    'actual_dirs': actual_dirs or [], 'files': files, 'error': error,
  }


def _dept(dept_id, name, items, error='', company_id='', company_name=''):
  return {'company_id': company_id, 'company_name': company_name,
          'dept_id': dept_id, 'dept_name': name, 'root_fid': 'root',
          'error': error, 'items': items}


def _snap(depts, period='2026-11'):
  return {'period_key': period, 'period_label': period, 'taken_at': NOW,
          'scope': 'dept', 'depts': depts, 'items': []}


def _status(verdict, dept_name, seq, company_id=None):
  for item in verdict['items']:
    if (item['dept_name'] == dept_name and item['seq'] == seq
        and (company_id is None or item.get('company_id') == company_id)):
      return item['status']
  raise AssertionError(f'没找到 {company_id or ""}/{dept_name}/{seq}')


def test_period_item_updated_when_expected_present():
  snap = _snap([_dept('d1', 'A1', [
    _item(2, '1.4.5', '能源月报', 'period',
          [_period_file('f1', EXPECTED)], latest=EXPECTED),
  ])])
  verdict = evaluate(snap, None)
  assert _status(verdict, 'A1', 2) == STATUS_UPDATED
  assert verdict['items'][0]['latest_period'] == EXPECTED


def test_period_item_updated_when_newer_than_expected():
  snap = _snap([_dept('d1', 'A1', [
    _item(2, '1.4.5', '能源月报', 'period',
          [_period_file('f1', '2026-11')], latest='2026-11'),
  ])])
  assert _status(evaluate(snap, None), 'A1', 2) == STATUS_UPDATED


def test_period_item_stale_when_only_older_period():
  snap = _snap([_dept('d1', 'A1', [
    _item(2, '1.4.5', '能源月报', 'period',
          [_period_file('f1', '2026-09')], latest='2026-09'),
  ])])
  verdict = evaluate(snap, None)
  assert _status(verdict, 'A1', 2) == STATUS_STALE
  assert verdict['items'][0]['latest_period'] == '2026-09', '页面要能写「最新为 2026-09」'


def test_period_item_never_baseline():
  # 按期提交型不依赖上期快照：首轮就能给真实结论
  snap = _snap([_dept('d1', 'A1', [
    _item(2, '1.4.5', '能源月报', 'period', [_period_file('f1', EXPECTED)]),
  ])])
  verdict = evaluate(snap, None)
  assert verdict['baseline'] is True
  assert _status(verdict, 'A1', 2) == STATUS_UPDATED


def test_period_item_missing_when_no_files():
  snap = _snap([_dept('d1', 'A1', [
    _item(2, '1.4.5', '能源月报', 'period', [], error='未查询到相关文件'),
  ])])
  assert _status(evaluate(snap, None), 'A1', 2) == STATUS_MISSING


def test_flat_item_baseline_without_previous():
  snap = _snap([_dept('d1', 'A1', [
    _item(5, '1.6.3.2', '整定表', 'flat', [_flat_file('f9', 'abc')]),
  ])])
  assert _status(evaluate(snap, None), 'A1', 5) == STATUS_BASELINE


def test_flat_item_updated_and_stale_against_previous():
  cur = _snap([_dept('d1', 'A1', [
    _item(5, '1.6.3.2', '整定表', 'flat', [_flat_file('f9', 'new')]),
  ])])
  prev = _snap([_dept('d1', 'A1', [
    _item(5, '1.6.3.2', '整定表', 'flat', [_flat_file('f9', 'old')]),
  ])], period='2026-10')
  verdict = evaluate(cur, prev)
  assert verdict['mode'] == 'cross'
  assert _status(verdict, 'A1', 5) == STATUS_UPDATED

  same = _snap([_dept('d1', 'A1', [
    _item(5, '1.6.3.2', '整定表', 'flat', [_flat_file('f9', 'old')]),
  ])])
  assert _status(evaluate(same, prev), 'A1', 5) == STATUS_STALE


def test_flat_new_file_counts_as_updated():
  cur = _snap([_dept('d1', 'A1', [
    _item(5, '1.6.3.2', '整定表', 'flat', [_flat_file('f_new', 'abc')]),
  ])])
  prev = _snap([_dept('d1', 'A1', [
    _item(5, '1.6.3.2', '整定表', 'flat', [_flat_file('f_old', 'abc')]),
  ])], period='2026-10')
  assert _status(evaluate(cur, prev), 'A1', 5) == STATUS_UPDATED


def test_flat_download_failure_is_failed_not_stale():
  snap = _snap([_dept('d1', 'A1', [
    _item(5, '1.6.3.2', '整定表', 'flat', [_flat_file('f9', '')]),
  ])])
  assert _status(evaluate(snap, None), 'A1', 5) == STATUS_FAILED


def test_dept_error_marks_items_failed():
  snap = _snap([_dept('d1', 'A1', [
    _item(2, '1.4.5', '能源月报', 'period', [], error='部门详情获取失败'),
  ], error='部门详情获取失败：TimeoutError')])
  verdict = evaluate(snap, None)
  assert _status(verdict, 'A1', 2) == STATUS_FAILED
  assert 'TimeoutError' in verdict['items'][0]['error']
  assert verdict['depts'][0]['error']


def test_legacy_tree_snapshot_is_not_a_baseline_reference():
  # 上期是旧单树快照（没有 depts），原地更新型不能拿它比 → 仍算基线
  legacy = {'period_key': '2026-10', 'period_label': '2026年10月',
            'taken_at': NOW, 'items': [
              {'seq': 5, 'number': '1.6.3.2', 'name': '整定表',
               'files': [{'fid': 'f9', 'sha256': 'old'}]},
            ]}
  cur = _snap([_dept('d1', 'A1', [
    _item(5, '1.6.3.2', '整定表', 'flat', [_flat_file('f9', 'new')]),
  ])])
  assert _status(evaluate(cur, legacy), 'A1', 5) == STATUS_BASELINE


def test_new_dept_is_baseline_not_updated():
  """上期没有的部门（新公司/新部门）：原地更新型记基线，不能误报「已更新」。"""
  cur = _snap([
    _dept('d1', 'A1', [_item(5, '1.6.3.2', '整定表', 'flat', [_flat_file('f9', 'new')])],
          company_id='c1', company_name='廊坊'),
    _dept('p1', 'A1', [_item(5, '1.6.3.2', '整定表', 'flat', [_flat_file('f9', 'new')])],
          company_id='c2', company_name='平湖'),
  ])
  prev = _snap([
    _dept('d1', 'A1', [_item(5, '1.6.3.2', '整定表', 'flat', [_flat_file('f9', 'old')])],
          company_id='c1', company_name='廊坊'),
  ], period='2026-10')
  verdict = evaluate(cur, prev)
  assert _status(verdict, 'A1', 5, 'c1') == STATUS_UPDATED
  assert _status(verdict, 'A1', 5, 'c2') == STATUS_BASELINE, '同名的另一家公司是新部门'
  by_company = {d['company_id']: d for d in verdict['depts']}
  assert by_company['c2']['baseline'] is True


def test_new_dept_period_item_still_judged():
  cur = _snap([
    _dept('d1', 'A1', [_item(2, '1.4.5', '能源月报', 'period',
                            [_period_file('f3', EXPECTED)])],
          company_id='c1', company_name='廊坊'),
    _dept('p1', 'A1', [_item(2, '1.4.5', '能源月报', 'period',
                            [_period_file('f1', EXPECTED)])],
          company_id='c2', company_name='平湖'),
  ])
  prev = _snap([_dept('d1', 'A1', [
    _item(2, '1.4.5', '能源月报', 'period', [_period_file('f2', '2026-09')]),
  ], company_id='c1', company_name='廊坊')], period='2026-10')
  verdict = evaluate(cur, prev)
  assert _status(verdict, 'A1', 2, 'c2') == STATUS_UPDATED, '按期提交型不依赖上期'
  text = summary_text(verdict)
  assert '2 家公司' in text


def test_dept_summary_and_text():
  snap = _snap([
    _dept('d1', 'A1', [
      _item(2, '1.4.5', '能源月报', 'period', [_period_file('f1', EXPECTED)]),
      _item(5, '1.6.3.2', '整定表', 'flat', [_flat_file('f9', 'abc')]),
    ]),
    _dept('d2', 'A2', [
      _item(2, '1.4.5', '能源月报', 'period', [], error='未查询到相关文件'),
    ]),
  ])
  verdict = evaluate(snap, None)
  assert verdict['total'] == 3 and verdict['dept_count'] == 2
  by_name = {d['dept_name']: d for d in verdict['depts']}
  assert by_name['A1']['counts'][STATUS_UPDATED] == 1
  assert by_name['A1']['counts'][STATUS_BASELINE] == 1
  assert by_name['A2']['counts'][STATUS_MISSING] == 1
  text = summary_text(verdict)
  assert '2 个部门' in text
  assert '已更新 1' in text and '未找到 1' in text and '基线 1' in text


def test_subset_verdict_keeps_dept_dimension():
  from service.doc_update.workflow import subset_verdict

  snap = _snap([
    _dept('d1', 'A1', [
      _item(2, '1.4.5', '能源月报', 'period', [_period_file('f1', EXPECTED)]),
      _item(5, '1.6.3.2', '整定表', 'flat', [_flat_file('f9', 'abc')]),
    ]),
    _dept('d2', 'A2', [
      _item(2, '1.4.5', '能源月报', 'period', [], error='未查询到相关文件'),
    ]),
  ])
  subset = subset_verdict(evaluate(snap, None), {2})
  assert subset['total'] == 2
  assert {d['dept_name'] for d in subset['depts']} == {'A1', 'A2'}
  assert all(i['seq'] == 2 for i in subset['items'])


def test_merge_dept_snapshot_by_dept_and_seq():
  base = _snap([
    _dept('d1', 'A1', [
      _item(2, '1.4.5', '能源月报', 'period', [_period_file('f1', EXPECTED)]),
      _item(5, '1.6.3.2', '整定表', 'flat', [_flat_file('f9', 'old')]),
    ]),
    _dept('d2', 'A2', [_item(2, '1.4.5', '能源月报', 'period', [])]),
  ])
  patch = _snap([_dept('d1', 'A1', [
    _item(2, '1.4.5', '能源月报', 'period', [_period_file('f2', '2026-11')]),
  ])])
  out = merge_snapshot(base, patch)
  by_dept = {d['dept_name']: d for d in out['depts']}
  assert by_dept['A1']['items'][0]['files'][0]['fid'] == 'f2', '命中项整项替换'
  assert by_dept['A1']['items'][1]['files'][0]['sha256'] == 'old', '其余项不动'
  assert 'A2' in by_dept, '未刷新的部门原样保留'


def test_actual_dirs_passthrough():
  """实际命中的目录名要带到判定结果（页面「实际目录」用）。"""
  snap = _snap([_dept('d1', 'A1', [
    _item(2, '1.4.5', '能源月报', 'period',
          [_period_file('f1', EXPECTED)], latest=EXPECTED,
          actual_dirs=['1.4.5能源管理报告（能源月报同步）', '8.1.2.3能源管理月报']),
  ])])
  verdict = evaluate(snap, None)
  assert verdict['items'][0]['actual_dirs'] == [
    '1.4.5能源管理报告（能源月报同步）', '8.1.2.3能源管理月报',
  ]


def test_known_numbers_cover_actual_folder_numbers():
  """剪枝目标 = 清单编号 ∪ 实际目录名里的编号，否则 8.1.2.3 走不到。"""
  from service.doc_update import share

  numbers = share.known_numbers()
  assert {'1.4.5', '1.2.3', '8.1.2.3'} <= set(numbers)
  assert share._compatible('8', numbers)
  assert share._compatible('8.1.2', numbers)
  assert not share._compatible('8.1.2.1', numbers)
  assert not share._compatible('9.9', numbers)


def main() -> int:
  tests = [v for k, v in sorted(globals().items()) if k.startswith('test_')]
  for fn in tests:
    fn()
    print(f'  ok  {fn.__name__}')
  print(f'全部通过：{len(tests)} 项')
  return 0


if __name__ == '__main__':
  raise SystemExit(main())
