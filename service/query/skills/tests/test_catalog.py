"""技能中心目录自测（纯 assert，不连库、不写真实技能目录）。

运行：`python -m service.query.skills.tests.test_catalog`
覆盖：主能力过滤、可见性（公开 + 本人）、来源/可编辑/启用标记、他人技能不可见、
详情字段（概述 / 使用场景 / 附带文档，随列表一次下发）、进程内快照（不按请求重扫、
扫描失败沿用上一份）。
"""

from service.query.base.capability import Capability
from service.query.skills import catalog, discover


def test_main_capabilities() -> None:
  caps = [
    Capability(id='skill.chart', name='图表', description='d', owner=''),
    Capability(id='skill.chart.line', name='折线', description='d', owner=''),
    Capability(id='skill.mine', name='我的', description='d', owner='u1'),
    Capability(id='mgmt.query_x', name='x', description='x'),
  ]
  ids = [c.id for c in catalog.main_capabilities(caps)]
  assert ids == ['skill.chart', 'skill.mine'], ids
  print('  [ok] 主能力过滤（保留 skill.<name>，排除子能力与非 skill 平台）')


def test_list_catalog() -> None:
  orig = catalog.discover_skills
  catalog.discover_skills = lambda: [
    Capability(id='skill.chart', name='图表', description='画图。支持多种图型。',
               when_to_use='要把数据画成图时使用；例：「画个折线图。」',
               category='chart', domain='viz', kind='资源', owner=''),
    Capability(id='skill.chart.line', name='折线', description='子能力', owner=''),
    Capability(id='skill.mine', name='我的技能', description='自建', category='custom',
               domain='custom', kind='资源', owner='u1'),
    Capability(id='skill.other', name='他人技能', description='别人的', owner='u2'),
  ]
  try:
    items = catalog.list_catalog('u1', {'mine'})
    names = {i['name'] for i in items}
    assert names == {'chart', 'mine'}, names
    assert 'chart.line' not in names and 'other' not in names

    by = {i['name']: i for i in items}
    assert by['chart']['title'] == '图表' and by['chart']['description'] == '画图。支持多种图型。'
    assert by['chart']['category'] == 'chart' and by['chart']['domain'] == 'viz'
    assert by['chart']['editable'] is False and by['chart']['enabled'] is True
    assert by['mine']['editable'] is True and by['mine']['enabled'] is False

    # 详情字段随列表一次下发：概述取首句、使用场景取 frontmatter、附带文档聚合子能力
    assert by['chart']['overview'] == '画图。', by['chart']['overview']
    assert by['chart']['when_to_use'].startswith('要把数据画成图时使用')
    assert by['chart']['references'] == [
      {'type': 'line', 'name': '折线', 'description': '子能力'},
    ], by['chart']['references']
    assert 'skill_md' not in by['chart']

    # 自建技能没写 when_to_use → 回退描述剩余句；单句描述则为空串
    assert by['mine']['when_to_use'] == ''

    # 公开技能不受 disabled 集合影响（只对自建技能生效）
    again = {i['name']: i for i in catalog.list_catalog('u1', {'mine', 'chart'})}
    assert again['chart']['enabled'] is True
    assert again['mine']['enabled'] is False

    # 未登录看不到任何人的自建技能，但公开技能仍在
    anon = {i['name'] for i in catalog.list_catalog('', set())}
    assert anon == {'chart'}, anon
  finally:
    catalog.discover_skills = orig
  print('  [ok] 列表可见性 + 详情字段（概述 / 使用场景 / 附带文档，无 SKILL.md 原文）')


def test_when_to_use_fallback() -> None:
  """没写 when_to_use 时回退描述剩余句；单句描述回退为空串。"""
  orig = catalog.discover_skills
  catalog.discover_skills = lambda: [
    Capability(id='skill.multi', name='多句', description='第一句。第二句说明场景。',
               owner=''),
    Capability(id='skill.single', name='单句', description='只有一句没有句末标点',
               owner=''),
  ]
  try:
    by = {i['name']: i for i in catalog.list_catalog('u1', set())}
    assert by['multi']['overview'] == '第一句。', by['multi']['overview']
    assert by['multi']['when_to_use'] == '第二句说明场景。', by['multi']['when_to_use']
    assert by['single']['overview'] == '只有一句没有句末标点'
    assert by['single']['when_to_use'] == ''
  finally:
    catalog.discover_skills = orig
  print('  [ok] 使用场景回退（描述剩余句；单句为空）')


def test_snapshot() -> None:
  """快照：扫一次后取值不变；扫描失败保留上一份、不清空。"""
  n1 = discover.refresh_skills_cache()
  assert len(discover.discover_skills_cached()) == n1
  assert len(discover.discover_skills_cached()) == n1  # 二次读取不重扫

  orig = discover.discover_skills

  def _boom():
    raise UnicodeDecodeError('utf-8', b'\xff', 0, 1, 'invalid start byte')

  discover.discover_skills = _boom
  try:
    n2 = discover.refresh_skills_cache()
    assert n2 == n1, (n2, n1)
    assert len(discover.discover_skills_cached()) == n1
  finally:
    discover.discover_skills = orig
  print('  [ok] 进程内快照（读取不重扫；扫描失败沿用上一份）')


def main() -> None:
  print('skills catalog self-test')
  test_main_capabilities()
  test_list_catalog()
  test_when_to_use_fallback()
  test_snapshot()
  print('全部通过')


if __name__ == '__main__':
  main()
