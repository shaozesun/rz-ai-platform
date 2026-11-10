"""技能中心目录 —— 列表 / 详情的只读层（不写盘、不触发 agent 重建）。

数据源直接扫 `skills/` 目录（`discover_skills()`），**不依赖平台是否已注册**：
技能中心页面可能在首次 Agent 构建之前被打开，走全局注册表会漏技能。

可见性口径：
- 公开技能（owner=''）所有人可见、只读（不可停用，共享图按用户重建的代价不值）；
- 自建技能（owner=<uid>）只对本人可见，可停用（开关状态存 Mongo `skill_states`）。
"""

import logging

from service.query.base.capability import Capability
from service.query.skills import client
from service.query.skills.discover import discover_skills
from service.query.skills.frontmatter import split_skill_markdown

logger = logging.getLogger(__name__)


def main_capabilities(caps: list[Capability]) -> list[Capability]:
  """只留主能力（`skill.<name>`），排除 references 子能力（`skill.<name>.<stem>`）。"""
  return [c for c in caps if c.id.startswith('skill.') and c.id.count('.') == 1]


def list_catalog(user_id: str, disabled: set[str]) -> list[dict]:
  """列出当前用户可见的技能：公开技能 + 本人自建技能（他人技能不出现）。

  Args:
    user_id: 当前用户 id（未登录为空串）。
    disabled: 当前用户已停用的技能名集合（只对自建技能生效）。

  Returns:
    每项 {name, title, description, category, domain, kind, owner, editable, enabled}。
  """
  items: list[dict] = []
  for cap in main_capabilities(discover_skills()):
    owner = cap.owner or ''
    if owner and owner != (user_id or ''):
      continue
    name = cap.id.split('.', 1)[1]
    items.append({
      'name': name,
      'title': cap.name,
      'description': cap.description,
      'category': cap.category,
      'domain': cap.domain,
      'kind': cap.kind,
      'owner': owner,
      'editable': bool(owner),
      'enabled': owner == '' or name not in disabled,
    })
  return items


def skill_detail(name: str, user_id: str) -> dict | None:
  """技能详情（SKILL.md 全文 + references 索引）。

  不存在、或技能归属他人 → 返回 None（接口层映射 404，不泄露归属）。
  """
  resolved = client.resolve_skill_dir(name)
  if resolved is None:
    return None
  skill_dir, owner = resolved
  if owner and owner != (user_id or ''):
    return None
  try:
    skill_md = (skill_dir / 'SKILL.md').read_text(encoding='utf-8')
  except OSError as e:
    logger.warning('读取技能失败 %s: %s', name, e)
    return None

  parsed = split_skill_markdown(skill_md)
  meta = parsed[0] if parsed is not None else {}
  category = str(meta.get('category') or '')
  return {
    'name': name,
    'title': str(meta.get('title') or name),
    'description': str(meta.get('description') or ''),
    'category': category,
    'domain': str(meta.get('domain') or category),
    'kind': str(meta.get('kind') or '资源'),
    'owner': owner,
    'editable': bool(owner),
    'skill_md': skill_md,
    'references': client._references_index(skill_dir),
  }
