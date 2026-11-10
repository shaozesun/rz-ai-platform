"""期号解析与目录形态判定自检（纯函数、不连库不联网）。

跑法：`python -m service.doc_update.tests.test_period`
"""

from datetime import datetime, timedelta, timezone

from service.doc_update import period as p

NOW = datetime(2026, 11, 3, tzinfo=timezone.utc)


def _file(segments, create_time=None):
  return {'segments': segments, 'create_time': create_time}


def _epoch(year, month=6, day=1):
  return int(datetime(year, month, day, tzinfo=timezone.utc).timestamp() * 1000)


def test_year_month_variants():
  assert p.parse_period(['2026', '9月']) == '2026-09'
  assert p.parse_period(['2026年9月']) == '2026-09'
  assert p.parse_period(['2026-09']) == '2026-09'
  assert p.parse_period(['2026/09']) == '2026-09'
  assert p.parse_period(['2026_9']) == '2026-09'
  assert p.parse_period(['2026.9']) == '2026-09'


def test_deepest_period_wins():
  assert p.parse_period(['2026', '9月', '2026-11']) == '2026-11'
  assert p.parse_period(['2026-08', '10月']) == '2026-10'


def test_month_only_uses_fallback_year():
  assert p.parse_period(['9月'], 2025) == '2025-09'
  assert p.parse_period(['9月']) == '', '没有年份也没有 fallback 时解析不到'
  assert p.parse_period(['2025', '9月'], 2024) == '2025-09', '目录里的年份优先于 fallback'


def test_embedded_title_month_dir():
  """部门共享树里月份目录常带业务标题：…【2025年10月总第116期】。"""
  assert p.parse_period(['2025', '润泽国际信息港A-1能源报告【2025年10月总第116期】']) == '2025-10'
  assert p.parse_period(['能源报告（2026年9月）']) == '2026-09'
  assert p.parse_period(['2026-11更新']) == '', '横杠写法不做模糊匹配（文件名坑）'


def test_year_only_is_not_month_level():
  assert p.parse_period(['2026']) == ''
  assert p.parse_period(['2026年']) == ''
  assert p.parse_period([]) == ''
  assert p.parse_period(['其他目录']) == ''


def test_invalid_month_rejected():
  assert p.parse_period(['2026-13']) == ''
  assert p.parse_period(['13月'], 2026) == ''
  assert p.parse_period(['1999-09']) == '', '年份超出 2000~2100 不认'


def test_period_of_file_uses_create_year():
  file = _file(['9月'], _epoch(2025))
  assert p.period_of_file(file) == '2025-09'
  assert p.period_of_file(_file(['2026', '9月'], _epoch(2024))) == '2026-09'


def test_detect_layout():
  assert p.detect_layout([]) == p.LAYOUT_FLAT
  assert p.detect_layout([_file(['2026']), _file(['线上更新'])]) == p.LAYOUT_FLAT
  assert p.detect_layout([_file(['线上更新']), _file(['2026', '9月'])]) == p.LAYOUT_PERIOD


def test_pick_latest_period():
  files = [_file(['2026', '8月']), _file(['2026', '10月']), _file(['线上更新'])]
  assert p.pick_latest_period(files) == '2026-10'
  assert p.pick_latest_period([_file(['线上更新'])]) == ''


def test_expected_period_grace():
  assert p.expected_period(NOW) == '2026-10', '默认回退 1 月'
  assert p.expected_period(NOW, grace_months=0) == '2026-11'
  assert p.expected_period(NOW, grace_months=2) == '2026-09'
  assert p.expected_period(NOW - timedelta(days=300), grace_months=1) == '2025-12'
  assert p.expected_period(datetime(2027, 1, 15), grace_months=1) == '2026-12'


def main() -> int:
  tests = [v for k, v in sorted(globals().items()) if k.startswith('test_')]
  for fn in tests:
    fn()
    print(f'  ok  {fn.__name__}')
  print(f'全部通过：{len(tests)} 项')
  return 0


if __name__ == '__main__':
  raise SystemExit(main())
