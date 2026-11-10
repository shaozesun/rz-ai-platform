"""部门清单/注册表自检（纯 assert、不连库不联网）。

跑法：`python -m service.doc_update.tests.test_depts_registry`
"""

from config.settings import settings
from service.doc_update.depts import (
  configured_groups, configured_names, merge_columns, parse_names, parse_spec,
)


def test_parse_names_sorts_and_dedupes():
  assert parse_names('A10，A2, A1,,A1') == ['A1', 'A2', 'A10']
  assert parse_names('  A3 , A1 ') == ['A1', 'A3']
  assert parse_names('') == []


def test_configured_names_from_settings():
  names = configured_names()
  assert names[:3] == ['A1', 'A2', 'A3'], names
  assert 'A18' in names
  assert names == sorted(names, key=lambda n: (int(n[1:]), n))


def test_merge_columns_keeps_configured_column_when_cache_missing():
  columns = merge_columns(['A1', 'A2'], [
    {'dept_name': 'A2', 'dept_id': 'id2'},
    {'dept_name': 'A9', 'dept_id': 'id9'},
  ])
  assert [c['dept_name'] for c in columns] == ['A1', 'A2'], '列以配置为准，顺序不变'
  assert columns[0]['dept_id'] == '', '缓存缺也出列，页面显示 —'
  assert columns[1]['dept_id'] == 'id2'
  assert all(c['dept_name'] != 'A9' for c in columns), '缓存多出的名字不出列'


def test_merge_columns_carries_error():
  columns = merge_columns(['A1'], [
    {'dept_name': 'A1', 'dept_id': '', 'error': '未在部门清单中找到该部门'},
  ])
  assert columns[0]['error']


def test_parse_spec_groups_by_company():
  groups = parse_spec('廊坊:c1:A1,A2;平湖:c2:A1', 'default')
  assert groups == [
    {'company_id': 'c1', 'company_name': '廊坊', 'names': ['A1', 'A2']},
    {'company_id': 'c2', 'company_name': '平湖', 'names': ['A1']},
  ]
  # 两段 `companyId:部门`：标签用 id
  assert parse_spec('c9:A3') == [
    {'company_id': 'c9', 'company_name': 'c9', 'names': ['A3']},
  ]
  # 全角标点也要认
  assert parse_spec('平湖：c2：A2，A1') == [
    {'company_id': 'c2', 'company_name': '平湖', 'names': ['A1', 'A2']},
  ]


def test_parse_spec_legacy_single_company():
  """不带 `:` 的旧值按单公司处理，套 DOC_UPDATE_COMPANY_ID。"""
  assert parse_spec('A2,A1', 'c1') == [
    {'company_id': 'c1', 'company_name': '', 'names': ['A1', 'A2']},
  ]
  assert parse_spec('', 'c1') == []


def test_merge_columns_keeps_same_name_per_company():
  """A1 在两家公司各一份：缓存按 (company_id, 部门) 取，互不串。"""
  columns = merge_columns(
    parse_spec('廊坊:c1:A1;平湖:c2:A1'),
    [
      {'company_id': 'c1', 'dept_name': 'A1', 'dept_id': 'id-lf'},
      {'company_id': 'c2', 'dept_name': 'A1', 'dept_id': 'id-ph'},
    ],
  )
  assert [(c['company_id'], c['dept_id']) for c in columns] == [
    ('c1', 'id-lf'), ('c2', 'id-ph'),
  ]
  assert [c['company_name'] for c in columns] == ['廊坊', '平湖']


def test_merge_columns_legacy_cache_claimed_by_default_company():
  """旧缓存行没有 company_id → 认领给 DOC_UPDATE_COMPANY_ID。"""
  default = settings.DOC_UPDATE_COMPANY_ID
  columns = merge_columns(
    parse_spec(f'廊坊:{default}:A1'),
    [{'dept_name': 'A1', 'dept_id': 'id1'}],
  )
  assert columns[0]['dept_id'] == 'id1'


def test_default_config_covers_measured_companies():
  """默认清单 = 实测 5 家国内园区共 21 个部门（去重 12 个部门名）。"""
  groups = configured_groups()
  assert [g['company_name'] for g in groups] == ['廊坊', '平湖', '惠州', '佛山', '重庆']
  assert sum(len(g['names']) for g in groups) == 21
  assert len(configured_names()) == 12
  assert configured_names() == [
    'A1', 'A2', 'A3', 'A5', 'A6', 'A7', 'A8', 'A9', 'A10', 'A11', 'A12', 'A18',
  ]


def main() -> int:
  tests = [v for k, v in sorted(globals().items()) if k.startswith('test_')]
  for fn in tests:
    fn()
    print(f'  ok  {fn.__name__}')
  print(f'全部通过：{len(tests)} 项')
  return 0


if __name__ == '__main__':
  raise SystemExit(main())
