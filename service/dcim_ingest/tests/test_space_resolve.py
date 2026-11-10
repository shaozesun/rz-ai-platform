"""空间名解析自检（纯 assert，不连库，非零退出码）。

    python -m service.dcim_ingest.tests.test_space_resolve

锁住 `repository._kw_hit` / `_space_roots` 的三条规则（`query_alarms` 的 `space`
参数靠它们把「A1」落到唯一一个空间 guid 上，再走既有的递归子树 CTE）：

- 短代号按数字边界：`A1` 不命中 `A10` / `A18`（与 `_kw_match` 的 SQL 语义同源）；
- 多词 AND：「A1 101机房」= 楼 + 房间同时满足；
- 祖先折叠：候选里同时有楼和它下面的房间时只留最上层那颗，否则上层无法判断用哪个 guid。
"""
from service.dcim_ingest.repository import _kw_hit, _space_roots

_ROOT = '9999_9999_9999_9999'

# 取自生产库真实形态：园区名 → 数据中心 → 区域/楼层 → 房间，
# 且**房间名不含上级的「区域」段**（这是不能按名字 LIKE 过滤子空间的实测证据）。
_ROWS = [
  {'cluster': '廊坊A', 'guid': 'p1', 'name': '廊坊园区', 'parent_guid': _ROOT},
  {'cluster': '廊坊A', 'guid': 'a1', 'name': '廊坊园区A1数据中心', 'parent_guid': 'p1'},
  {'cluster': '廊坊A', 'guid': 'a10', 'name': '廊坊园区A10数据中心', 'parent_guid': 'p1'},
  {'cluster': '廊坊A', 'guid': 'a18', 'name': '廊坊园区A18数据中心', 'parent_guid': 'p1'},
  {'cluster': '廊坊A', 'guid': 'a1-m1', 'name': '廊坊园区A1数据中心M1模组', 'parent_guid': 'a1'},
  {'cluster': '廊坊A', 'guid': 'a1-room101',
   'name': '廊坊园区A1数据中心M1一层101机房', 'parent_guid': 'a1-m1'},
  {'cluster': '廊坊A', 'guid': 'a10-room101',
   'name': '廊坊园区A10数据中心M1一层101机房', 'parent_guid': 'a10'},
  {'cluster': '廊坊A', 'guid': 'a12', 'name': '廊坊园区A12数据中心', 'parent_guid': 'p1'},
  {'cluster': '廊坊A', 'guid': 'a12-strong',
   'name': '廊坊园区A12数据中心M1-3F-强电间2', 'parent_guid': 'a12'},
  # 生产实测：房间名以代号结尾（「…电池室A1」），只靠数字边界仍会跟着命中 A1
  {'cluster': '廊坊A', 'guid': 'a12-cell-a1',
   'name': '廊坊园区A12数据中心配电区域三层电池室A1', 'parent_guid': 'a12'},
  {'cluster': '重庆', 'guid': 'cq-p1', 'name': '重庆园区', 'parent_guid': _ROOT},
  {'cluster': '重庆', 'guid': 'cq-a1', 'name': '重庆园区A1数据中心', 'parent_guid': 'cq-p1'},
]


def _guids(rows):
  return sorted(r['guid'] for r in rows)


def _check_kw_hit():
  assert _kw_hit('廊坊园区A1数据中心', 'A1')
  assert not _kw_hit('廊坊园区A10数据中心', 'A1')
  assert not _kw_hit('廊坊园区A18数据中心', 'A1')
  assert not _kw_hit('廊坊园区A123数据中心', 'A1')
  assert _kw_hit('廊坊园区A1数据中心二层101机房', 'a1')       # 大小写不敏感
  # 非短代号走子串，行为与原 ILIKE 一致
  assert _kw_hit('廊坊园区A12数据中心M1-3F-强电间2', 'M1-3F')
  assert _kw_hit('廊坊园区A1数据中心M1一层101机房', '201机房') is False
  assert _kw_hit('廊坊园区A1数据中心M1一层201机房', '201机房')


def _check_building_root():
  """「A1」= 整栋楼：楼内的模组/房间被祖先折叠掉，A12 里以 A1 结尾的房间被「优先认楼」排除。"""
  rows = [r for r in _ROWS if r['cluster'] == '廊坊A']
  assert _guids(_space_roots(rows, 'A1')) == ['a1']
  # 楼那一行缺失时不能把「…A1数据中心M1模组」硬认成楼 —— 保留多候选、交给上层报歧义
  only_rooms = [r for r in rows if r['guid'] != 'a1']
  assert _guids(_space_roots(only_rooms, 'A1')) == ['a1-m1', 'a12-cell-a1']


def _check_cross_park_ambiguity():
  """不给园区时 A1 在两个园区各有一个 → 两个顶层候选（调用层据此报歧义）。"""
  assert _guids(_space_roots(_ROWS, 'A1')) == ['a1', 'cq-a1']


def _check_multi_token_and():
  """「A1 101机房」= 两个词同时命中；只给「101机房」则两栋楼各一间、必须多义。"""
  rows = [r for r in _ROWS if r['cluster'] == '廊坊A']
  assert _guids(_space_roots(rows, 'A1 101机房')) == ['a1-room101']
  assert _guids(_space_roots(rows, 'A1、101机房')) == ['a1-room101']   # 顿号同义
  assert _guids(_space_roots(rows, '101机房')) == ['a1-room101', 'a10-room101']


def _check_no_match_and_blank():
  assert _space_roots(_ROWS, '不存在的机房') == []
  assert _space_roots(_ROWS, '   ') == []
  assert _space_roots([], 'A1') == []


def _check_cycle_and_orphan():
  """脏数据不能让解析死循环：自环、断链祖先都当根处理。"""
  self_loop = [{'cluster': 'c', 'guid': 's1', 'name': '廊坊园区A1数据中心', 'parent_guid': 's1'}]
  assert _guids(_space_roots(self_loop, 'A1')) == ['s1']

  # 祖先行缺失（跨园区/已删）→ 判断不了是子节点，都当根；没有楼名就保留多候选，不硬认
  orphans = [
    {'cluster': 'c', 'guid': 'r1', 'name': '廊坊园区A12数据中心三层电池室A1', 'parent_guid': 'gone'},
    {'cluster': 'c', 'guid': 'r2', 'name': '廊坊园区A12数据中心一层配电室A1', 'parent_guid': 'gone'},
  ]
  assert _guids(_space_roots(orphans, 'A1')) == ['r1', 'r2']


def main():
  _check_kw_hit()
  _check_building_root()
  _check_cross_park_ambiguity()
  _check_multi_token_and()
  _check_no_match_and_blank()
  _check_cycle_and_orphan()
  print('space_resolve OK: 短代号边界 / 多词 AND / 祖先折叠 / 脏数据不死循环')


if __name__ == '__main__':
  main()
