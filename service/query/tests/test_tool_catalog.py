"""工具中心目录自检（纯 assert，不连库、不调外部接口）。

运行：`python -m service.query.tests.test_tool_catalog`
覆盖：平台分组与可见性（常驻/按需/模式门控）、技能按用户隔离、中文标签、
内置工具组的开关增删与标题映射。
"""

import contextlib

from config.settings import settings
from service.query import tool_catalog
from service.query.base.capability import Capability


@contextlib.contextmanager
def _switch(**kwargs):
  """临时改配置开关，退出时恢复。"""
  old = {k: getattr(settings, k) for k in kwargs}
  for key, value in kwargs.items():
    setattr(settings, key, value)
  try:
    yield
  finally:
    for key, value in old.items():
      setattr(settings, key, value)


_CAPS = [
  Capability(id='mgmt.query_my_info', name='我的账号信息', description='查当前账号',
             category='org', domain='person', required_perm='query:platform'),
  Capability(id='dcim_meta.query_alarms', name='告警查询', description='查动环告警',
             category='alarm', domain='alarm'),
  Capability(id='web.search', name='联网检索', description='搜索网页'),
  Capability(id='browser.navigate', name='打开页面', description='交互打开'),
  Capability(id='skill.chart', name='图表', description='画图', category='chart',
             domain='viz', owner=''),
  Capability(id='skill.chart.line', name='折线', description='子能力', category='chart',
             domain='viz', owner=''),
  Capability(id='skill.mine', name='我的技能', description='自建', owner='u1'),
  Capability(id='skill.other', name='他人技能', description='别人的', owner='u2'),
]


def test_summary() -> None:
  """一句话摘要：短句原样、句号截断、首行、去粗体、超长回退。"""
  summary = tool_catalog._summary
  assert summary('查当前账号') == '查当前账号'
  assert summary('第一句。第二句') == '第一句'
  assert summary('**粗体**开头\n参数：x') == '粗体开头'
  assert summary('') == '' and summary(None) == ''

  long_comma = '甲' * 40 + '，' + '乙' * 40
  out = summary(long_comma)
  assert out.endswith('…') and '乙' not in out and len(out) <= 61, out

  hard = summary('甲' * 80)
  assert hard.endswith('…') and len(hard) == 61, hard


def test_platform_groups() -> None:
  orig_discover = tool_catalog.discover_skills
  orig_browser = tool_catalog._browser_always_names
  tool_catalog.discover_skills = lambda: _CAPS
  tool_catalog._browser_always_names = lambda: {'browser__navigate'}
  try:
    groups = {g['key']: g for g in tool_catalog.platform_groups(_CAPS, 'u1')}
  finally:
    tool_catalog.discover_skills = orig_discover
    tool_catalog._browser_always_names = orig_browser

  assert list(groups) == ['mgmt', 'dcim_meta', 'web', 'browser', 'skill'], list(groups)
  assert groups['mgmt']['label'] == '综合管理平台'
  assert groups['web']['label'] == '联网' and groups['skill']['label'] == '技能'

  mgmt = {t['name']: t for t in groups['mgmt']['tools']}['mgmt__query_my_info']
  assert mgmt['category_label'] == '组织人事' and mgmt['domain_label'] == '人员'
  assert mgmt['required_perm'] == 'query:platform'
  assert mgmt['visibility'] == 'on_demand' and mgmt['mode_gated'] is False
  assert mgmt['summary'] == '查当前账号'

  web = groups['web']['tools'][0]
  assert web['visibility'] == 'mode_gated' and web['mode_gated'] is True

  browser = groups['browser']['tools'][0]
  assert browser['visibility'] == 'always'

  # 技能：公开父技能常驻可见、本人自建按需检索；子能力与他人技能不出现
  skills = {t['name']: t for t in groups['skill']['tools']}
  assert set(skills) == {'skill__chart', 'skill__mine'}, set(skills)
  assert skills['skill__chart']['visibility'] == 'always'
  assert skills['skill__mine']['visibility'] == 'on_demand'


def test_builtin_groups() -> None:
  with _switch(
    AGENT_ANALYSIS_ENABLED=False, VIDEO_AGENT_ENABLED=False, SCHEDULED_TASKS_ENABLED=False,
    DOC_UPDATE_CHECK_ENABLED=False, LARK_ENABLED=False, DCIM_ENABLED=False,
  ):
    keys = {g['key'] for g in tool_catalog.builtin_groups()}
  always_on = {'base', 'skill_manage', 'todo', 'upload', 'history', 'incident'}
  assert always_on <= keys, keys
  assert not ({'analysis', 'video', 'schedule', 'doc_update', 'feishu', 'event_alarm'} & keys), keys

  with _switch(AGENT_ANALYSIS_ENABLED=True):
    groups = {g['key']: g for g in tool_catalog.builtin_groups()}
  analysis = [t['name'] for t in groups['analysis']['tools']]
  assert analysis == [
    'analysis_load', 'analysis_exec', 'analysis_row_detail', 'analysis_read_file',
    'analysis_write_file', 'analysis_str_replace', 'analysis_ls', 'analysis_glob',
    'analysis_grep',
  ], analysis
  assert groups['analysis']['tools'][0]['title'] == '物化数据'
  assert groups['analysis']['switch'] == 'AGENT_ANALYSIS_ENABLED'
  base = {t['name']: t for t in groups['base']['tools']}
  assert base['rag_knowledge_search']['title'] == '知识库检索'
  assert base['rag_knowledge_search']['visibility'] == 'always'
  assert base['rag_knowledge_search']['summary'].startswith('检索知识库')


def main() -> None:
  print('tool catalog self-test')
  test_summary()
  print('  [ok] 一句话摘要（句号截断/首行/去粗体/超长回退）')
  test_platform_groups()
  print('  [ok] 平台分组（顺序/标签/可见性/技能隔离）')
  test_builtin_groups()
  print('  [ok] 内置分组（开关增删/标题映射/常驻可见）')
  print('全部通过')


if __name__ == '__main__':
  main()
