"""零阶保持自检（纯 assert，不连库，非零退出码）。

    python -m service.dcim_ingest.tests.test_carry

锁住 `wide_writer.merge_values` 的一条规则：状态只记**真实采样**，
被带出来的值**不续期** —— 60 秒内补、超时留空，不再无限累积成整行。
"""
from service.dcim_ingest.wide_writer import merge_values

_GAP = 60_000     # DCIM_CARRY_MAX_SECONDS=60 → 毫秒


def _check_carry_within_gap():
  vals, state = merge_values(None, ts=1_000, values={'a': 1.0}, max_gap_ms=_GAP)
  assert vals == {'a': 1.0}, vals

  # 间隔正好 60s → 仍带（边界左闭右闭）
  vals, _ = merge_values(state, ts=61_000, values={}, max_gap_ms=_GAP)
  assert vals == {'a': 1.0}, vals

  # 超过 60s → 如实留空
  vals, _ = merge_values(state, ts=61_001, values={}, max_gap_ms=_GAP)
  assert vals == {}, vals


def _check_carried_value_does_not_renew():
  """旧实现的 bug：带出来的值被当成新起点 → 永远续期。"""
  _, state = merge_values(None, ts=0, values={'a': 1.0}, max_gap_ms=_GAP)

  vals, state = merge_values(state, ts=10_000, values={}, max_gap_ms=_GAP)
  assert vals == {'a': 1.0}, vals
  assert state['a'] == (1.0, 0), state      # 未续期

  vals, state = merge_values(state, ts=20_000, values={}, max_gap_ms=_GAP)
  assert vals == {'a': 1.0}, vals

  # 距**真实采样** 70s → 失效（旧实现会一直带下去）
  vals, _ = merge_values(state, ts=70_000, values={}, max_gap_ms=_GAP)
  assert vals == {}, vals


def _check_real_sample_renews():
  _, state = merge_values(None, ts=0, values={'a': 1.0}, max_gap_ms=_GAP)
  _, state = merge_values(state, ts=30_000, values={'a': 2.0}, max_gap_ms=_GAP)
  assert state['a'] == (2.0, 30_000), state

  vals, _ = merge_values(state, ts=70_000, values={}, max_gap_ms=_GAP)
  assert vals == {'a': 2.0}, vals            # 距真实采样 40s，仍在窗口内


def _check_same_ts_merge():
  _, state = merge_values(None, ts=5_000, values={'a': 1.0}, max_gap_ms=_GAP)
  vals, state = merge_values(state, ts=5_000, values={'b': 2.0}, max_gap_ms=_GAP)
  assert vals == {'a': 1.0, 'b': 2.0}, vals
  assert state == {'a': (1.0, 5_000), 'b': (2.0, 5_000)}, state


def _check_none_does_not_overwrite():
  _, state = merge_values(None, ts=0, values={'a': 1.0}, max_gap_ms=_GAP)
  vals, state = merge_values(state, ts=1_000, values={'a': None}, max_gap_ms=_GAP)
  assert vals == {'a': 1.0}, vals
  assert state['a'] == (1.0, 0), state       # None 不刷新采样时刻


def _check_no_shared_mutation():
  """纯函数：输入状态不被改写（多台设备各持一份，不会互相污染）。"""
  s1 = {'a': (1.0, 0)}
  _, s1_new = merge_values(s1, ts=1_000, values={'b': 2.0}, max_gap_ms=_GAP)
  assert s1 == {'a': (1.0, 0)}, s1
  assert s1_new == {'a': (1.0, 0), 'b': (2.0, 1_000)}, s1_new


def main():
  _check_carry_within_gap()
  _check_carried_value_does_not_renew()
  _check_real_sample_renews()
  _check_same_ts_merge()
  _check_none_does_not_overwrite()
  _check_no_shared_mutation()
  print('carry OK: 只带 60s 内真实采样，带出的值不续期')


if __name__ == '__main__':
  main()
