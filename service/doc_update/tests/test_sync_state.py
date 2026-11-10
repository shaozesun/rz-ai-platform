"""同步租约自检（纯 assert、不连 Redis）：互斥、进度、释放、token 防误删、旧值容错。

跑法：`python -m service.doc_update.tests.test_sync_state`
"""

import json

from service.doc_update import sync_state


class FakeClient:
  """只实现 `set/get/eval` 三件套；eval 按脚本里是否有 DEL 区分续期与释放。"""

  def __init__(self):
    self.data: dict[str, str] = {}
    self.calls: list[tuple] = []
    self.fail_set = False

  def set(self, key, value, nx=False, ex=None):
    if self.fail_set:
      raise RuntimeError('redis down')
    self.calls.append((key, value, nx, ex))
    if nx and key in self.data:
      return False
    self.data[key] = value
    return True

  def get(self, key):
    return self.data.get(key)

  def eval(self, script, numkeys, key, token, *args):
    raw = self.data.get(key)
    if raw is None:
      return 0
    try:
      cur = json.loads(raw)
    except (ValueError, TypeError):
      return 0
    if not isinstance(cur, dict) or cur.get('token') != token:
      return 0
    if 'DEL' in script:
      self.data.pop(key, None)
    else:
      self.data[key] = args[0]
    return 1


class FakeManager:
  def __init__(self):
    self.client = FakeClient()

  def key(self, name):
    return f'rz:{name}'


def _fake() -> FakeManager:
  fake = FakeManager()
  sync_state.redis_manager = fake
  return fake


def test_acquire_is_mutually_exclusive():
  _fake()
  lease = sync_state.acquire('manual', 'u1')
  assert lease is not None
  assert sync_state.acquire('manual', 'u2') is None, '已有租约时第二次抢锁必须失败'
  info = sync_state.read()
  assert info and info['mode'] == 'manual' and info['user_id'] == 'u1'


def test_release_frees_lock():
  _fake()
  lease = sync_state.acquire('manual', 'u1')
  lease.release()
  assert sync_state.read() is None
  assert sync_state.acquire('manual', 'u1') is not None


def test_update_writes_progress():
  fake = _fake()
  lease = sync_state.acquire('manual', 'u1')
  lease.update(3, 300, 'A7 · 1.4.5 能源管理报告')
  info = sync_state.read()
  assert (info['done'], info['total']) == (3, 300)
  assert info['current'] == 'A7 · 1.4.5 能源管理报告'
  assert info['started_at'] and info['started_at'] > 0
  _, payload, _, ex = fake.client.calls[0]
  assert ex == sync_state.LEASE_TTL
  assert json.loads(payload)['token'] == lease.token


def test_old_lease_cannot_delete_new_one():
  _fake()
  old = sync_state.acquire('manual', 'u1')
  old.release()
  new = sync_state.acquire('manual', 'u2')
  old.release()
  assert sync_state.read() is not None, '被 kill 的旧任务不能删掉新租约'
  assert sync_state.read()['user_id'] == 'u2'
  new.release()


def test_legacy_value_is_tolerated():
  fake = _fake()
  fake.client.data['rz:doc_update:sync'] = 'u123'
  info = sync_state.read()
  assert info['mode'] == 'legacy' and info['user_id'] == 'u123'
  assert info['done'] == 0 and info['total'] == 0


def test_redis_down_fails_open():
  fake = _fake()
  fake.client.fail_set = True
  lease = sync_state.acquire('manual', 'u1')
  assert lease is not None, 'Redis 不可用应与旧行为一致（不上锁继续）'
  assert sync_state.read() is None
  lease.release()


def main() -> int:
  tests = [v for k, v in sorted(globals().items()) if k.startswith('test_')]
  for fn in tests:
    fn()
    print(f'  ok  {fn.__name__}')
  print(f'全部通过：{len(tests)} 项')
  return 0


if __name__ == '__main__':
  raise SystemExit(main())
