"""技能中心目录自测（纯 assert，不连库、不写真实技能目录）。

运行：`python -m service.query.skills.tests.test_catalog`
覆盖：主能力过滤、可见性（公开 + 本人）、来源/可编辑/启用标记、他人技能不可见。
"""

import shutil
import tempfile
from pathlib import Path

from service.query.base.capability import Capability
from service.query.skills import catalog, client


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
    Capability(id='skill.chart', name='图表', description='画图', category='chart',
               domain='viz', kind='资源', owner=''),
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
    assert by['chart']['title'] == '图表' and by['chart']['description'] == '画图'
    assert by['chart']['category'] == 'chart' and by['chart']['domain'] == 'viz'
    assert by['chart']['editable'] is False and by['chart']['enabled'] is True
    assert by['mine']['editable'] is True and by['mine']['enabled'] is False

    # 公开技能不受 disabled 集合影响（只对自建技能生效）
    again = {i['name']: i for i in catalog.list_catalog('u1', {'mine', 'chart'})}
    assert again['chart']['enabled'] is True
    assert again['mine']['enabled'] is False

    # 未登录看不到任何人的自建技能，但公开技能仍在
    anon = {i['name'] for i in catalog.list_catalog('', set())}
    assert anon == {'chart'}, anon
  finally:
    catalog.discover_skills = orig
  print('  [ok] 列表可见性（公开 + 本人；他人技能不出现；公开不受停用影响）')


def test_skill_detail() -> None:
  tmp = Path(tempfile.mkdtemp())
  skill_dir = tmp / 'demo'
  (skill_dir / 'references').mkdir(parents=True)
  (skill_dir / 'SKILL.md').write_text(
    '---\nname: demo\ntitle: 演示技能\ndescription: 一句说明\n'
    'category: demo\ndomain: demo\nkind: 资源\n---\n\n# 正文\n\n步骤…\n',
    encoding='utf-8',
  )
  (skill_dir / 'references' / 'guide.md').write_text(
    '---\nname: 指南\ndescription: 子文档说明\n---\n\n内容\n', encoding='utf-8',
  )

  orig = client.resolve_skill_dir
  client.resolve_skill_dir = lambda name: (skill_dir, 'u1') if name == 'demo' else None
  try:
    # 他人技能不可见（越权统一 None → 接口 404，不泄露归属）
    assert catalog.skill_detail('demo', 'u2') is None
    assert catalog.skill_detail('nope', 'u1') is None

    out = catalog.skill_detail('demo', 'u1')
    assert out is not None
    assert out['name'] == 'demo' and out['title'] == '演示技能'
    assert out['owner'] == 'u1' and out['editable'] is True
    assert '# 正文' in out['skill_md']
    assert out['references'] == [
      {'type': 'guide', 'name': '指南', 'description': '子文档说明'},
    ], out['references']
  finally:
    client.resolve_skill_dir = orig
    shutil.rmtree(tmp, ignore_errors=True)
  print('  [ok] 详情可见性（他人 404 / 本人带 SKILL.md 与 references）')


def main() -> None:
  print('skills catalog self-test')
  test_main_capabilities()
  test_list_catalog()
  test_skill_detail()
  print('全部通过')


if __name__ == '__main__':
  main()
