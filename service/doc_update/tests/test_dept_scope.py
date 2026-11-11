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


def _item(seq, number, name, layout, files, latest='', error='', actual_dirs=None,
          period_only=False):
  return {
    'seq': seq, 'number': number, 'name': name, 'cadence': '月度', 'note': '',
    'layout': layout, 'period_only': period_only, 'expected_period': EXPECTED,
    'latest_period': latest, 'actual_dirs': actual_dirs or [], 'files': files,
    'error': error,
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


def test_year_folder_not_pruned_and_matched():
  """四位年份归档目录不参与剪枝；能源月报在 …/能源月报/2026年/10月 下必须能命中。"""
  from service.doc_update import catalog, fetch, period, share

  assert share._is_period_dir('2026')
  assert share._is_period_dir('2026.10')
  assert not share._is_period_dir('1.4')
  assert not share._is_period_dir('8.1.2')
  assert not share._is_period_dir('9.9')

  item = next(i for i in catalog.MONTHLY_ITEMS if i.seq == 2)
  base = {
    'fid': 'f1', 'topic': '能源报告2026年10月.pptx', 'parent_id': 'dir',
    'path': '1.规划管理 / 1.4精益化能源管理 / 1.4.5能源管理报告（能源月报同步）'
            ' / 能源月报 / 2026年 / 10月',
    'segments': ['1.规划管理', '1.4精益化能源管理',
                 '1.4.5能源管理报告（能源月报同步）', '能源月报', '2026年', '10月'],
    'create_time': 0, 'size': 100,
  }
  hits = fetch.match_item(item, [base])
  assert len(hits) == 1, hits
  assert hits[0]['match_dir'] == '能源月报', hits[0]['match_dir']
  assert period.detect_layout(hits) == period.LAYOUT_PERIOD

  # 通用别名：目录不带编号写法也能命中
  bare = dict(base, fid='f2', segments=['能源月报', '2026年', '9月'])
  assert len(fetch.match_item(item, [bare])) == 1


def test_energy_name_period_formats():
  """能源月报期号常只写在文件名里：覆盖实测到的各部门写法。"""
  from service.doc_update import period

  cases = {
    '能源报告【2026年10月总第128期】.pptx': '2026-10',
    '2026年10月度能源报告.xlsx': '2026-10',
    '润泽国际信息港A-3能源报告-2026-10.pdf': '2026-10',
    '廊坊A1能源报告-2026-10月（新版）.pdf': '2026-10',
    '能源报告（20260930)-9月.pdf': '2026-09',
    '能源报告【20261031】.pdf': '2026-10',
    '能源报告【202603】.pdf': '2026-03',
    '能源报告【202604】.xlsx': '2026-04',
    '能源报告2018-202604.xlsx': '2026-04',
    '10、2026年10月能源报告.xlsx': '2026-10',
    '2026年A18能源报告4.30-4月份.pdf': '2026-04',
    '2025年A6能源报告1.31-1月份.pdf': '2025-01',
    '(2026年10月）.pdf': '2026-10',
    '2026年9月惠州电量数据.pdf': '2026-09',
  }
  for name, want in cases.items():
    got = period.name_period(name)
    assert got == want, (name, got, want)
  assert period.name_period('能源报告-2025年.xlsx') == ''
  assert period.name_period('1、一月') == ''
  # 纯月份靠目录年份；`A-2 月度` 不能被当成 2 月
  assert period.name_period('润泽国际信息港 A-2 月度能源报告-10月.xlsx', 2026) == '2026-10'
  assert period.name_period('月度能源报告-12月.pdf', 2025) == '2025-12'


def test_energy_period_of_file_ignores_create_time():
  """2025 期的文件可能是 2026 年补传的：年份只能取目录，不能取 create_time。"""
  from datetime import datetime

  from service.doc_update import period

  ts = datetime(2026, 6, 1).timestamp() * 1000
  f = {'segments': ['1.4.5能源管理报告', '能源月报', '2025年'],
       'topic': '润泽国际信息港 A-2 月度能源报告-10月.xlsx',
       'create_time': ts}
  assert period.period_of_file(f) == '', '默认仍只看目录名'
  assert period.period_of_file(f, from_name=True) == '2025-10'

  # 目录能解析时以目录为准（比文件名可靠）
  g = {'segments': ['能源月报', '2026年', '10月'],
       'topic': '能源报告-09月.pdf', 'create_time': 0}
  assert period.period_of_file(g, from_name=True) == '2026-10'


def test_energy_catalog_period_only():
  from service.doc_update import catalog

  energy = next(i for i in catalog.MONTHLY_ITEMS if i.seq == 2)
  assert energy.period_only is True
  assert all(not i.period_only for i in catalog.MONTHLY_ITEMS if i.seq != 2)


def test_segments_year_from_directory_names():
  from service.doc_update import period

  assert period._segments_year(['1.4.5能源管理报告', '2026年输出物']) == 2026
  assert period._segments_year(['2026年数据']) == 2026
  assert period._segments_year(['2026年度']) == 2026
  assert period._segments_year(['2026']) == 2026
  assert period._segments_year([]) is None


def test_energy_period_only_judged_without_hash():
  snap = _snap([_dept('d1', 'A1', [
    _item(2, '1.4.5', '能源月报', 'period',
          [_period_file('f1', EXPECTED)], latest=EXPECTED, period_only=True),
  ])])
  assert _status(evaluate(snap, None), 'A1', 2) == STATUS_UPDATED

  old = _snap([_dept('d1', 'A1', [
    _item(2, '1.4.5', '能源月报', 'period',
          [_period_file('f1', '2026-09')], latest='2026-09', period_only=True),
  ])])
  assert _status(evaluate(old, None), 'A1', 2) == STATUS_STALE


def test_energy_unparsed_files_fail_with_reason():
  snap = _snap([_dept('d1', 'A1', [
    _item(2, '1.4.5', '能源月报', 'period',
          [_period_file('f1', '')], period_only=True),
  ])])
  verdict = evaluate(snap, None)
  item = verdict['items'][0]
  assert item['status'] == STATUS_FAILED, '不能含糊成「未更新」'
  assert '解析出期号' in item['error']
  assert item['files'][0]['status'] == STATUS_FAILED


def test_energy_stray_unparsed_file_does_not_flip_item():
  snap = _snap([_dept('d1', 'A1', [
    _item(2, '1.4.5', '能源月报', 'period',
          [_period_file('f1', EXPECTED), _period_file('f2', '')],
          latest=EXPECTED, period_only=True),
  ])])
  verdict = evaluate(snap, None)
  assert _status(verdict, 'A1', 2) == STATUS_UPDATED
  by_fid = {f['fid']: f for f in verdict['items'][0]['files']}
  assert by_fid['f1']['status'] == STATUS_UPDATED
  assert by_fid['f2']['status'] == STATUS_FAILED


def test_plain_period_item_keeps_old_stale_behavior():
  """其余 24 项不变：无期号的文件仍按「未更新」处理。"""
  snap = _snap([_dept('d1', 'A1', [
    _item(2, '1.4.5', '能源月报', 'period',
          [_period_file('f1', EXPECTED), _period_file('f2', '')]),
  ])])
  verdict = evaluate(snap, None)
  by_fid = {f['fid']: f for f in verdict['items'][0]['files']}
  assert by_fid['f2']['status'] == STATUS_STALE


def test_item19_catalog_aliases():
  """2.3.2.6 补真实名称别名：无编号目录也要能命中。"""
  from service.doc_update import catalog

  item19 = next(i for i in catalog.MONTHLY_ITEMS if i.seq == 19)
  assert '客户IT库房应急卡' in item19.folders
  assert '客户库房应急卡' in item19.folders
  assert not [i for i in catalog.MONTHLY_ITEMS if i.seq not in (2, 19) and i.folders], \
    '本次只给 seq 19 补别名'


def test_item19_matches_unnumbered_folder():
  from service.doc_update import catalog, fetch

  item19 = next(i for i in catalog.MONTHLY_ITEMS if i.seq == 19)
  a1 = ['廊坊-A1-数据中心运维管理体系', '2.运行维护管理', '2.3值班管理',
        '2.3.2数据中心监控室管理', '客户IT库房应急卡领取记录表', '2026年', '10月']
  f = {'fid': 'f1', 'topic': 'IT库房应急卡.pdf', 'parent_id': 'd',
       'path': ' / '.join(a1), 'segments': a1, 'create_time': 0, 'size': 1}
  hits = fetch.match_item(item19, [f])
  assert len(hits) == 1
  assert hits[0]['match_dir'] == '客户IT库房应急卡领取记录表'

  # A10 形态：无编号目录挂在年份目录下（2026年 不参与编号剪枝）
  a10 = ['廊坊-A10-数据中心运维管理体系', '2.运行维护管理', '2.3值班管理',
         '2.3.2数据中心监控室管理', '2026年', '客户IT库房应急卡领取登记表']
  g = {'fid': 'f2', 'topic': '2026年10月-客户IT库房应急卡领取登记表.pdf',
       'parent_id': 'd', 'path': ' / '.join(a10), 'segments': a10,
       'create_time': 0, 'size': 1}
  hits10 = fetch.match_item(item19, [g])
  assert len(hits10) == 1
  assert hits10[0]['match_dir'] == '客户IT库房应急卡领取登记表'


def test_item19_not_confused_with_item15():
  """2.3.2.2 的「应急卡使用登记表」不能被 2.3.2.6 的别名吃进来。"""
  from service.doc_update import catalog, fetch

  item19 = next(i for i in catalog.MONTHLY_ITEMS if i.seq == 19)
  item15 = next(i for i in catalog.MONTHLY_ITEMS if i.seq == 15)
  segs = ['廊坊-A8-数据中心运维管理体系', '2.运行维护管理', '2.3值班管理',
          '2.3.2数据中心监控室管理', '2.3.2.2润泽科技-廊坊-A8-应急卡使用登记表',
          '2026年', '10月']
  f = {'fid': 'f1', 'topic': '10月应急卡使用登记表.pdf', 'parent_id': 'd',
       'path': ' / '.join(segs), 'segments': segs, 'create_time': 0, 'size': 1}
  assert fetch.match_item(item19, [f]) == []
  assert len(fetch.match_item(item15, [f])) == 1


def main() -> int:
  tests = [v for k, v in sorted(globals().items()) if k.startswith('test_')]
  for fn in tests:
    fn()
    print(f'  ok  {fn.__name__}')
  print(f'全部通过：{len(tests)} 项')
  return 0


if __name__ == '__main__':
  raise SystemExit(main())
