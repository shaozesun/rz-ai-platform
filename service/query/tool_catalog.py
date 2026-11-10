"""工具中心目录 —— 只读聚合「智能体当前能调用的全部工具」。

两类来源：
1. **平台能力**：已注册平台（mgmt / dcim_meta / web / browser / skill）的能力，
   按注册序分平台成组；技能单独走 `discover_skills()`（页面可能在首次 Agent 构建前
   打开，全局注册表会漏技能），且按用户隔离（他人自建技能不出现）。
2. **代码内置工具**：声明式表 `_BUILTIN_GROUPS`，运行时按 `(module, attr[, factory])`
   动态取 `.name` / `.description`。

⚠️ **同步基准是 `service/agent/agent_service.py::_rebuild`**（唯一的工具装配处）。
加减内置工具时必须同步改本文件。**禁止扫描 `tools/*.py` 的 `@tool`**：
`tools/mock_ops_tools.py` 里有 5 个未注入 Agent 的工具，扫出来的不是真实能力。

本模块是**纯聚合层**：不 import `service/agent/agent_service.py`（防循环依赖），
只读配置与能力元数据，不调外部接口、不写库。
"""

import importlib
import logging
from dataclasses import dataclass

from config.settings import settings
from service.query.base.manifest import (
  category_display_name,
  domain_display_name,
  platform_definition,
  platform_display_name,
)
from service.query.base.platform import is_mode_gated
from service.query.base.tools_factory import capability_to_tool_name
from service.query.skills.catalog import main_capabilities
from service.query.skills.discover import discover_skills

logger = logging.getLogger(__name__)

# 页面展示用的一句话描述上限（`description` 是写给模型的长文，不能直接上页面）
_SUMMARY_MAX = 60
_SUMMARY_SEPS = ('。', '？', '！')


def _summary(description: str) -> str:
  """从工具描述里取一句话摘要：首行 → 首个句末标点 → 超长按标点回退加省略号。"""
  text = (description or '').strip()
  if not text:
    return ''
  first = text.splitlines()[0].strip().replace('**', '')
  for sep in _SUMMARY_SEPS:
    idx = first.find(sep)
    if idx != -1:
      first = first[:idx]
      break
  first = first.strip()
  if len(first) <= _SUMMARY_MAX:
    return first
  cut = first[:_SUMMARY_MAX]
  for sep in ('，', '、', '；', ',', ';'):
    pos = cut.rfind(sep)
    if pos >= _SUMMARY_MAX // 2:
      return cut[:pos] + '…'
  return cut + '…'


# 工具名 → 中文短标题（内置工具没有 capability.name，只能手工登记；缺失回退工具名）
TOOL_TITLES: dict[str, str] = {
  'rag_knowledge_search': '知识库检索',
  'get_current_time': '当前时间',
  'send_notification': '发送通知',
  'analysis_load': '物化数据',
  'analysis_exec': '沙箱分析',
  'analysis_row_detail': '取记录明细',
  'analysis_read_file': '读工作区文件',
  'analysis_write_file': '写工作区文件',
  'analysis_str_replace': '替换文件片段',
  'analysis_ls': '列出工作区文件',
  'analysis_glob': '按模式找文件',
  'analysis_grep': '搜文件内容',
  'skill_manage': '技能管理',
  'stage_skill_script': '准备技能脚本',
  'write_todos': '更新任务清单',
  'list_uploaded_files': '列上传文件',
  'read_uploaded_file': '读上传文件',
  'load_uploaded_table_as_dataset': '上传表格转数据集',
  'copy_uploaded_file_to_workspace': '上传文件转工作区',
  'analyze_uploaded_image': '识别上传图片',
  'history_search': '检索历史归档',
  'history_read': '读历史归档原文',
  'incident_report': '数据中心事件统计表',
  'incident_progress': '事件进展全记录',
  'schedule_task': '定时任务管理',
  'event_alarm_link': '事件关联告警',
  'doc_update_check': '文档更新检查',
  'video_create': 'PPT 生成讲解视频',
  'video_create_from_text': '文字生成讲解视频',
  'video_status': '查询视频任务',
  'video_pages': '查看视频分页',
  'video_update_page': '改单页解说词',
  'video_update_pages': '批量改解说词',
  'video_set_options': '设置视频选项',
  'video_rerender': '重新合成视频',
  'fetch_group_messages': '读飞书群消息',
  'fetch_p2p_messages': '读飞书私聊记录',
  'list_joined_chats': '列已加入的群',
  'list_group_members': '列群成员',
}


@dataclass(frozen=True)
class _BuiltinGroup:
  """内置工具组声明。`switch` 为空 = 常开；`entries` 为 (模块, 属性, 是否工厂)。"""

  key: str
  label: str
  description: str
  switch: str | None
  entries: tuple[tuple[str, str, bool], ...]


_BUILTIN_GROUPS: tuple[_BuiltinGroup, ...] = (
  _BuiltinGroup(
    'base', '基础能力', '知识库检索、当前时间、站内通知，始终可用', None,
    (
      ('tools.rag_tools', 'rag_knowledge_search', False),
      ('tools.mock_tools', 'get_current_time', False),
      ('tools.mock_ops_tools', 'send_notification', False),
    ),
  ),
  _BuiltinGroup(
    'analysis', '数据分析',
    '把平台数据全量物化到会话工作区，再用 pandas 做统计、出图、写文件',
    'AGENT_ANALYSIS_ENABLED',
    # build_analysis_tools() 已包含文件操作（read/write/str_replace/ls/glob/grep），
    # 不要再单独声明 build_file_tools()，否则同一批工具重复出现。
    (('tools.analysis_tools', 'build_analysis_tools', True),),
  ),
  _BuiltinGroup(
    'skill_manage', '技能管理', '在对话里创建 / 查看 / 改进技能，并把技能自带脚本放进工作区', None,
    (
      ('tools.skill_tools', 'skill_manage', False),
      ('tools.skill_tools', 'stage_skill_script', False),
    ),
  ),
  _BuiltinGroup(
    'todo', '任务清单', '计划模式下维护任务进度清单（展示在输入框上方）', None,
    (('tools.todo_tools', 'write_todos', False),),
  ),
  _BuiltinGroup(
    'upload', '文件上传', '查看 / 读取会话里上传的文件，把上传表格转成分析数据集', None,
    (('tools.upload_tools', 'build_upload_tools', True),),
  ),
  _BuiltinGroup(
    'history', '历史回读', '按需回读被上下文压缩归档的旧对话原文', None,
    (('tools.history_tools', 'build_history_tools', True),),
  ),
  _BuiltinGroup(
    'incident', '事件统计', '从飞书园区群通报生成数据中心事件统计表，以及单个事件的完整进展', None,
    (
      ('tools.incident_tools', 'incident_report', False),
      ('tools.incident_tools', 'incident_progress', False),
    ),
  ),
  _BuiltinGroup(
    'schedule', '定时任务', '用自然语言创建 / 管理定时执行的 Agent 任务', 'SCHEDULED_TASKS_ENABLED',
    (('tools.schedule_task_tool', 'schedule_task', False),),
  ),
  _BuiltinGroup(
    'event_alarm', '事件告警关联', '把事件工单与动环告警按时间 / 设备匹配起来', 'DCIM_ENABLED',
    (('tools.event_alarm_tools', 'event_alarm_link', False),),
  ),
  _BuiltinGroup(
    'doc_update', '文档更新检查', '按月核对各部门共享文档是否已更新', 'DOC_UPDATE_CHECK_ENABLED',
    (('tools.doc_update_tools', 'doc_update_check', False),),
  ),
  _BuiltinGroup(
    'video', '讲解视频', '用上传的 PPT 或一段文字生成逐页解说的讲解视频', 'VIDEO_AGENT_ENABLED',
    (('tools.agent_video_tools', 'build_agent_video_tools', True),),
  ),
  _BuiltinGroup(
    'feishu', '飞书消息', '读取飞书群消息、私聊记录与群成员', 'LARK_ENABLED',
    (
      ('tools.feishu_tools', 'fetch_group_messages', False),
      ('tools.feishu_tools', 'fetch_p2p_messages', False),
      ('tools.feishu_tools', 'list_joined_chats', False),
      ('tools.feishu_tools', 'list_group_members', False),
    ),
  ),
)


def _browser_always_names() -> set[str]:
  """交互浏览的 7 个工具名（开关关时为空集）。"""
  if not settings.BROWSER_EXPLORE_ENABLED:
    return set()
  try:
    from service.query.browser.interactive import tool_names
    return tool_names()
  except Exception as e:  # noqa: BLE001 —— 可选模块，缺依赖不应让页面炸掉
    logger.warning('工具目录：读取交互浏览工具名失败 %s', e)
    return set()


def _cap_item(cap, visibility: str) -> dict:
  return {
    'name': capability_to_tool_name(cap.id),
    'title': cap.name,
    'description': cap.description,
    'summary': _summary(cap.description),
    'category_label': category_display_name(cap.category) if cap.category else '',
    'domain_label': domain_display_name(cap.domain) if cap.domain else '',
    'required_perm': cap.required_perm or '',
    'visibility': visibility,
    'mode_gated': is_mode_gated(cap.id),
  }


def _visibility(cap, browser_always: set[str]) -> str:
  """常驻可见 / 按需检索 / 仅特定模式 —— 与 agent_service._rebuild 的口径一致。"""
  if is_mode_gated(cap.id):
    return 'mode_gated'
  if capability_to_tool_name(cap.id) in browser_always:
    return 'always'
  return 'on_demand'


def _skills_group(user_id: str) -> dict | None:
  """技能组：公开技能（常驻可见）+ 本人自建技能（按需检索），他人技能不出现。"""
  tools: list[dict] = []
  for cap in main_capabilities(discover_skills()):
    owner = cap.owner or ''
    if owner and owner != user_id:
      continue
    # 公开父技能在构建时常驻可见（skill__chart 等），自建技能走 tool_search
    tools.append(_cap_item(cap, 'always' if not owner else 'on_demand'))
  if not tools:
    return None
  return {
    'key': 'skill',
    'label': platform_display_name('skill'),
    'description': platform_definition('skill'),
    'tools': tools,
  }


def platform_groups(caps: list, user_id: str = '') -> list[dict]:
  """平台能力分组（纯函数，便于单测）。

  Args:
    caps: 全部已注册能力（`all_capabilities()` 的返回值）。
    user_id: 当前用户，用于过滤他人自建技能。

  Returns:
    每组 `{key, label, description, tools}`；技能组按注册位置插入，空平台跳过。
  """
  uid = user_id or ''
  browser_always = _browser_always_names()

  order: list[str] = []
  buckets: dict[str, list] = {}
  for cap in caps:
    platform = cap.id.split('.', 1)[0]
    if platform not in buckets:
      buckets[platform] = []
      order.append(platform)
    if platform != 'skill':
      buckets[platform].append(cap)

  groups: list[dict] = []
  for platform in order:
    if platform == 'skill':
      group = _skills_group(uid)
      if group:
        groups.append(group)
      continue
    tools = [_cap_item(cap, _visibility(cap, browser_always)) for cap in buckets[platform]]
    if not tools:
      continue
    groups.append({
      'key': platform,
      'label': platform_display_name(platform),
      'description': platform_definition(platform),
      'tools': tools,
    })
  return groups


def _load_builtin_group(spec: _BuiltinGroup) -> dict | None:
  """按声明表加载一组内置工具；开关关闭或导入失败返回 None（页面不能因此炸掉）。"""
  if spec.switch and not getattr(settings, spec.switch, False):
    return None
  tools: list[dict] = []
  for module_path, attr, is_factory in spec.entries:
    try:
      module = importlib.import_module(module_path)
      obj = getattr(module, attr)
      loaded = list(obj()) if is_factory else [obj]
    except Exception as e:  # noqa: BLE001 —— 可选模块，缺依赖只跳过该组
      logger.warning('工具目录：加载 %s.%s 失败，跳过「%s」组 (%s)', module_path, attr, spec.label, e)
      return None
    for tool in loaded:
      name = getattr(tool, 'name', '') or ''
      description = getattr(tool, 'description', '') or ''
      tools.append({
        'name': name,
        'title': TOOL_TITLES.get(name, name),
        'description': description,
        'summary': _summary(description),
        'category_label': '',
        'domain_label': '',
        'required_perm': '',
        'visibility': 'always',
        'mode_gated': False,
      })
  if not tools:
    return None
  return {
    'key': spec.key,
    'label': spec.label,
    'description': spec.description,
    'switch': spec.switch or '',
    'tools': tools,
  }


def builtin_groups() -> list[dict]:
  """代码内置工具分组（按 `_BUILTIN_GROUPS` 声明序，开关关闭的组整体消失）。"""
  groups: list[dict] = []
  for spec in _BUILTIN_GROUPS:
    group = _load_builtin_group(spec)
    if group:
      groups.append(group)
  return groups


def build_groups(user_id: str = '') -> list[dict]:
  """工具中心数据源：平台分组（含技能）→ 内置分组。

  注册表为空时才调 `register_enabled_platforms()`：`register_platform()` 会
  `reset_index()` 清空检索向量缓存，每请求都调会让下一次 tool_search 重建索引。
  """
  from service.query.base.platform import all_capabilities, registered_platforms

  if not registered_platforms():
    from service.query.bootstrap import register_enabled_platforms
    register_enabled_platforms()
  return platform_groups(all_capabilities(), user_id) + builtin_groups()
