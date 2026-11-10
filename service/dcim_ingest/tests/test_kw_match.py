"""关键词匹配自检（纯 assert，不连库，非零退出码）。

    python -m service.dcim_ingest.tests.test_kw_match

锁住 `repository._kw_match` / `_kw_any` 的一条规则：
短代号（1~3 字母 + 1~3 数字）按**词边界**匹配，`A1` 不再命中 `A18/A12`；
其余关键词（中文、长串、带符号）维持子串 `ILIKE`，行为逐字不变。
"""
import re

from service.dcim_ingest.repository import _kw_any, _kw_match


def _hits(pattern: str, text: str) -> bool:
  return re.search(pattern, text, re.IGNORECASE) is not None


def _check_code_keyword():
  sql, args = _kw_match('s.name', 'A1')
  assert sql == 's.name ~* %s', sql
  assert args == [r'(^|[^0-9])A1([^0-9]|$)'], args
  pat = args[0]
  assert not _hits(pat, '廊坊园区A18数据中心二层201机房')
  assert not _hits(pat, '廊坊园区A12数据中心二层101机房')
  assert not _hits(pat, '廊坊园区A123数据中心')
  assert _hits(pat, '廊坊园区A1数据中心二层101机房')
  assert _hits(pat, 'A1')                        # 整串就是代号
  assert _hits(pat, 'a1机柜')                     # 大小写不敏感 + 中文边界


def _check_code_keywords_self():
  _, args = _kw_match('x', 'A12')
  pat = args[0]
  assert _hits(pat, '廊坊园区A12数据中心')
  assert not _hits(pat, '廊坊园区A1数据中心')
  assert not _hits(pat, '廊坊园区A123数据中心')


def _check_substring_unchanged():
  for kw in ('温度', '201机房', 'M1-3F', 'A-1'):
    sql, args = _kw_match('p.name', kw)
    assert sql == 'p.name ILIKE %s', (kw, sql)
    assert args == [f'%{kw}%'], (kw, args)


def _check_kw_any():
  frags, args = _kw_any('A1', 'a.point_name', 's.name')
  assert frags == 'a.point_name ~* %s OR s.name ~* %s', frags
  assert args == [r'(^|[^0-9])A1([^0-9]|$)'] * 2, args

  frags, args = _kw_any('温度', 'a.point_name', 's.name')
  assert frags == 'a.point_name ILIKE %s OR s.name ILIKE %s', frags
  assert args == ['%温度%', '%温度%'], args


def main():
  _check_code_keyword()
  _check_code_keywords_self()
  _check_substring_unchanged()
  _check_kw_any()
  print('kw_match OK: 短代号按词边界、其余仍走子串 ILIKE')


if __name__ == '__main__':
  main()
