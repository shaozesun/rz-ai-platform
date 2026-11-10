"""单项刷新合并自检（纯 assert、不连库）：按 seq 替换、其余项原样、计数重算。

跑法：`python -m service.doc_update.tests.test_merge`
"""

from datetime import datetime, timezone

from service.doc_update.fetch import merge_snapshot

OLD = datetime(2026, 9, 10, tzinfo=timezone.utc)
NEW = datetime(2026, 10, 20, tzinfo=timezone.utc)


def _item(seq, name, files):
  return {'seq': seq, 'number': f'n{seq}', 'name': name, 'cadence': '月度',
          'note': '', 'files': files, 'error': '' if files else '未找到'}


def _file(fid, sha='abc'):
  return {'fid': fid, 'file_name': f'{fid}.pdf', 'sha256': sha,
          'last_changed_at': OLD}


def _snap(taken_at, items):
  return {'period_key': '2026-10', 'period_label': '2026年10月',
          'taken_at': taken_at, 'items': items, 'file_count': 0, 'elapsed': 9}


def test_replaces_matched_item():
  base = _snap(NEW, [_item(1, 'A', [_file('f1')]), _item(2, 'B', [_file('f2')])])
  patch = _snap(NEW, [_item(2, 'B', [_file('f2'), _file('f3')])])
  out = merge_snapshot(base, patch)
  by_seq = {i['seq']: i for i in out['items']}
  assert by_seq[2] is patch['items'][0], '命中项应整项替换为本次结果'


def test_keeps_other_items_untouched():
  base = _snap(NEW, [_item(1, 'A', [_file('f1')]), _item(2, 'B', [_file('f2')])])
  patch = _snap(NEW, [_item(1, 'A', [_file('f1', 'new')])])
  out = merge_snapshot(base, patch)
  # 未命中的第 2 项：连 files 里的 last_changed_at 都不能动
  assert out['items'][1] == base['items'][1]
  assert out['items'][1]['files'][0]['last_changed_at'] == OLD


def test_recounts_and_takes_patch_time():
  base = _snap(OLD, [_item(1, 'A', [_file('f1')]), _item(2, 'B', [_file('f2')])])
  patch = _snap(NEW, [_item(1, 'A', [_file('f1'), _file('f1b'), _file('f1c')])])
  patch['elapsed'] = 1.2
  out = merge_snapshot(base, patch)
  assert out['file_count'] == 4, out['file_count']
  assert out['taken_at'] == NEW, '整期快照时间应推进到本次'
  assert out['elapsed'] == 1.2
  assert out['period_key'] == base['period_key']


def test_empty_base_returns_patch():
  # 本期还没有整期快照时只做临时比对，不该凭空造出 24 个空项
  patch = _snap(NEW, [_item(3, 'C', [_file('f3')])])
  assert merge_snapshot(None, patch) is patch
  assert merge_snapshot({}, patch) is patch


def main() -> int:
  tests = [v for k, v in sorted(globals().items()) if k.startswith('test_')]
  for fn in tests:
    fn()
    print(f'  ok  {fn.__name__}')
  print(f'全部通过：{len(tests)} 项')
  return 0


if __name__ == '__main__':
  raise SystemExit(main())
