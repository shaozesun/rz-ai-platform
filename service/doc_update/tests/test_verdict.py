"""快照比对自检（纯 assert、不连库）：状态判定 / 关键词消歧 / 增删识别。

跑法：`python -m service.doc_update.tests.test_verdict`
"""

from datetime import datetime, timedelta, timezone

from service.doc_update.catalog import MONTHLY_ITEMS, DocItem
from service.doc_update.fetch import match_item
from service.doc_update.verdict import (
  MODE_BASELINE,
  MODE_CROSS,
  STATUS_BASELINE,
  STATUS_FAILED,
  STATUS_MISSING,
  STATUS_STALE,
  STATUS_UPDATED,
  evaluate,
  summary_text,
)

NOW = datetime(2026, 10, 30, tzinfo=timezone.utc)


def _file(fid, sha, warn='', days_ago=5):
  return {
    'fid': fid, 'file_name': f'{fid}.pdf', 'path': 'x', 'size': 100,
    'download_size': 100, 'version': '', 'update_time': '', 'upload_time': '',
    'sha256': sha, 'error': '' if sha else 'boom', 'warn': warn,
    'last_changed_at': NOW - timedelta(days=days_ago) if sha else '',
  }


def _snap(period, items):
  return {'period_key': period, 'period_label': period, 'taken_at': NOW,
          'items': items}


def _item(number, name, files, error=''):
  return {'seq': 1, 'number': number, 'name': name, 'cadence': '月度',
          'note': '', 'files': files, 'error': error}


def test_baseline():
  latest = _snap('2026-10', [_item('1.2.2', 'A', [_file('f1', 'aaa')])])
  v = evaluate(latest, None, NOW)
  assert v['baseline'] is True
  assert v['items'][0]['status'] == STATUS_BASELINE
  assert v['counts'][STATUS_BASELINE] == 1


def test_updated_and_stale():
  prev = _snap('2026-09', [_item('1.2.2', 'A', [_file('f1', 'old')]),
                           _item('1.4.5', 'B', [_file('f2', 'same')])])
  latest = _snap('2026-10', [_item('1.2.2', 'A', [_file('f1', 'new')]),
                             _item('1.4.5', 'B', [_file('f2', 'same')])])
  v = evaluate(latest, prev, NOW)
  assert v['baseline'] is False
  status = {i['number']: i['status'] for i in v['items']}
  assert status['1.2.2'] == STATUS_UPDATED, status
  assert status['1.4.5'] == STATUS_STALE, status
  assert len(v['changes']['changed']) == 1
  assert v['changes']['changed'][0]['file_name'] == 'f1.pdf'


def test_missing_vs_failed():
  prev = _snap('2026-09', [_item('2.3.2.6', 'M', [])])
  latest = _snap('2026-10', [_item('2.3.2.6', 'M', []),
                             _item('1.2.2', 'A', [_file('f1', '')])])
  v = evaluate(latest, prev, NOW)
  status = {i['number']: i['status'] for i in v['items']}
  assert status['2.3.2.6'] == STATUS_MISSING, status
  assert status['1.2.2'] == STATUS_FAILED, status
  # 下载失败/未找到都不得计入「未更新」
  assert v['counts'][STATUS_STALE] == 0


def test_partial_failure_still_counts_updated():
  prev = _snap('2026-09', [_item('1.6.6.1', 'X', [_file('f1', 'old'),
                                                  _file('f2', 'old2')])])
  latest = _snap('2026-10', [_item('1.6.6.1', 'X', [_file('f1', 'new'),
                                                    _file('f2', '')])])
  v = evaluate(latest, prev, NOW)
  assert v['items'][0]['status'] == STATUS_UPDATED


def test_warnings_collected():
  latest = _snap('2026-10', [_item('1.2.2', 'A', [_file('f1', 'aaa', warn='大小不符')])])
  v = evaluate(latest, None, NOW)
  assert v['warnings'] and v['warnings'][0]['warn'] == '大小不符'


def test_added_and_removed_fids():
  prev = _snap('2026-09', [_item('A', 'A', [_file('gone', 'x')])])
  latest = _snap('2026-10', [_item('A', 'A', [_file('fresh', 'y')])])
  v = evaluate(latest, prev, NOW)
  assert v['changes']['added_fids'] == ['fresh']
  assert v['changes']['removed_fids'] == ['gone']
  # 新增 fid 没有上期 sha，按「已更新」处理
  assert v['items'][0]['status'] == STATUS_UPDATED


def test_changed_days_ago():
  latest = _snap('2026-10', [_item('A', 'A', [_file('f1', 'aaa', days_ago=12)])])
  v = evaluate(latest, None, NOW)
  assert v['items'][0]['changed_days_ago'] == 12


def test_415_keyword_disambiguation():
  files = [
    {'fid': '1', 'topic': '4.1.5 供配电隐患排查表', 'parent_id': 'p', 'path': ''},
    {'fid': '2', 'topic': '4.1.5 制冷隐患排查表', 'parent_id': 'p', 'path': ''},
    {'fid': '3', 'topic': '4.1.5 智能化隐患排查表', 'parent_id': 'p', 'path': ''},
    {'fid': '4', 'topic': '4.1.5 班组级隐患排查记录表', 'parent_id': 'p', 'path': ''},
    {'fid': '5', 'topic': '4.1.6 电伴热安全检查表', 'parent_id': 'p', 'path': ''},
  ]
  item = DocItem(29, ('4.1.5',), '供配电隐患排查表', '月度', 'PDF', ('供配电',))
  hits = match_item(item, files)
  assert [h['fid'] for h in hits] == ['1'], hits


def test_number_on_directory():
  """部门共享树里编号在目录上，文件名是业务标题（不带编号）。"""
  files = [
    {'fid': '1', 'topic': '润泽国际信息港A-1能源报告【2025年10月总第116期】.pdf',
     'parent_id': 'p', 'path': '', 'segments': ['1.4精益化能源管理', '1.4.5能源管理报告（能源月报同步）']},
    {'fid': '2', 'topic': '别的表.xlsx', 'parent_id': 'p', 'path': '',
     'segments': ['1.4精益化能源管理', '1.4.4能源监视']},
  ]
  item = DocItem(2, ('1.4.5',), '能源月报', '月度', 'PPT', ('能源',))
  assert [h['fid'] for h in match_item(item, files)] == ['1']


def test_actual_folder_names_match_same_item():
  """清单名是模糊叫法：同一份月报在真实树里有三处写法，全部算这一项。"""
  item = next(i for i in MONTHLY_ITEMS if i.seq == 2)
  files = [
    {'fid': 'a', 'topic': '润泽国际信息港A-1能源报告【2026年9月总第128期】.pdf',
     'parent_id': 'p', 'path': '', 'segments': [
       '1.规划管理', '1.4精益化能源管理', '1.4.5能源管理报告（能源月报同步）']},
    {'fid': 'b', 'topic': '容量管理报告.pdf', 'parent_id': 'p', 'path': '',
     'segments': ['1.规划管理', '1.2容量管理', '1.2.3容量管理报告（能源月报同步）']},
    {'fid': 'c', 'topic': '能源管理月报2026-08.xlsx', 'parent_id': 'p', 'path': '',
     'segments': ['8.报告表管理', '8.1报告管理', '8.1.2能源管理类报告', '8.1.2.3能源管理月报']},
    {'fid': 'd', 'topic': '耗电量周报.xlsx', 'parent_id': 'p', 'path': '',
     'segments': ['8.报告表管理', '8.1报告管理', '8.1.2能源管理类报告', '8.1.2.1耗电量周报']},
  ]
  hits = match_item(item, files)
  assert [h['fid'] for h in hits] == ['a', 'b', 'c'], hits
  assert [h['match_dir'] for h in hits] == [
    '1.4.5能源管理报告（能源月报同步）',
    '1.2.3容量管理报告（能源月报同步）',
    '8.1.2.3能源管理月报',
  ], hits  # 取最深层命中的目录，便于页面写「实际目录」


def test_match_dir_empty_when_only_file_name_matches():
  item = next(i for i in MONTHLY_ITEMS if i.seq == 6)
  files = [{'fid': '1', 'topic': '1.6.6.2供配电程序文档.pdf', 'parent_id': 'p',
            'path': '', 'segments': ['1.6.6供配电程序文档目录']}]
  hits = match_item(item, files)
  assert [h['fid'] for h in hits] == ['1']
  assert hits[0]['match_dir'] == ''


def test_directory_number_still_respects_prefix_rule():
  files = [
    {'fid': '1', 'topic': 'x.xlsx', 'parent_id': 'p', 'path': '',
     'segments': ['1.6.6.10别的目录']},
    {'fid': '2', 'topic': 'x.xlsx', 'parent_id': 'p', 'path': '',
     'segments': ['1.6.6.1供配电程序文档目录']},
  ]
  item = DocItem(6, ('1.6.6.1',), '供配电程序文档目录', '月度')
  assert [h['fid'] for h in match_item(item, files)] == ['2']


def test_number_prefix_not_confused():
  files = [{'fid': '1', 'topic': '1.6.6.10 别的表', 'parent_id': 'p', 'path': ''}]
  item = DocItem(6, ('1.6.6.1',), '供配电程序文档目录', '月度')
  assert match_item(item, files) == []


def test_same_month_recheck_still_updated():
  """同月重跑：参照始终是上期，文件已更新就一直显示已更新，不翻成未更新。"""
  prev = _snap('2026-09', [_item('1.2.2', 'A', [_file('f1', 'old')])])
  first = _snap('2026-10', [_item('1.2.2', 'A', [_file('f1', 'new')])])
  again = _snap('2026-10', [_item('1.2.2', 'A', [_file('f1', 'new')])])
  for snap in (first, again):
    v = evaluate(snap, prev, NOW)
    assert v['mode'] == MODE_CROSS
    assert v['items'][0]['status'] == STATUS_UPDATED
    assert v['counts'][STATUS_STALE] == 0


def test_no_reference_is_baseline():
  latest = _snap('2026-10', [_item('1.2.2', 'A', [_file('f1', 'aaa')])])
  v = evaluate(latest, None, NOW)
  assert v['mode'] == MODE_BASELINE
  assert v['baseline'] is True
  text = summary_text(v)
  # 首期只说基线，不能说「未更新」
  assert '未更新' not in text, text
  assert '已建立基线' in text


def main() -> int:
  tests = [v for k, v in sorted(globals().items()) if k.startswith('test_')]
  for fn in tests:
    fn()
    print(f'  ok  {fn.__name__}')
  print(f'全部通过：{len(tests)} 项')
  return 0


if __name__ == '__main__':
  raise SystemExit(main())
