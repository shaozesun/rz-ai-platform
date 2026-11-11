"""技能中心目录 —— 列表（含详情字段）的只读层（不写盘、不触发 agent 重建）。

数据源是 `skills/` 目录的**进程内快照**（`discover_skills_cached()`，启动时建立、
写盘/删除经 `reload_skills()` 刷新），**不依赖平台是否已注册**：技能中心页面可能在
首次 Agent 构建之前被打开，走全局注册表会漏技能；也不按请求扫盘（目录抖动会 500）。

可见性口径：
- 公开技能（owner=''）所有人可见、只读（不可停用，共享图按用户重建的代价不值）；
- 自建技能（owner=<uid>）只对本人可见，可停用（开关状态存 Mongo `skill_states`）。
"""

import logging

from service.query.base.capability import Capability
# 别名保持模块级符号名不变：测试 monkeypatch 的是 `catalog.discover_skills`
from service.query.skills.discover import discover_skills_cached as discover_skills

logger = logging.getLogger(__name__)

_SENTENCE_END = '。！？!?'


def _first_sentence(text: str) -> str:
  """取首句（按句末标点切，保留标点）；没有句末标点返回整段。"""
  s = (text or '').strip()
  for i, ch in enumerate(s):
    if ch in _SENTENCE_END:
      return s[:i + 1]
  return s


def _rest_sentences(text: str) -> str:
  """首句之后的剩余文本；只有一句（或无句末标点）返回空串。"""
  s = (text or '').strip()
  for i, ch in enumerate(s):
    if ch in _SENTENCE_END:
      return s[i + 1:].strip()
  return ''


def _references_by_skill(caps: list[Capability]) -> dict[tuple[str, str], list[dict]]:
  """从快照里的 references 子能力（`skill.<name>.<stem>`）聚合附带文档索引。"""
  index: dict[tuple[str, str], list[dict]] = {}
  for cap in caps:
    parts = cap.id.split('.')
    if len(parts) != 3 or parts[0] != 'skill':
      continue
    key = (cap.owner or '', parts[1])
    index.setdefault(key, []).append({
      'type': parts[2],
      'name': cap.name,
      'description': cap.description,
    })
  return index


def main_capabilities(caps: list[Capability]) -> list[Capability]:
  """只留主能力（`skill.<name>`），排除 references 子能力（`skill.<name>.<stem>`）。"""
  return [c for c in caps if c.id.startswith('skill.') and c.id.count('.') == 1]


def list_catalog(user_id: str, disabled: set[str]) -> list[dict]:
  """列出当前用户可见的技能（含详情字段）：公开技能 + 本人自建技能（他人不出现）。

  取数走进程内快照，不扫盘；概述 / 使用场景 / 附带文档全部由快照里的能力元数据推导，
  前端打开详情不再发请求。

  Args:
    user_id: 当前用户 id（未登录为空串）。
    disabled: 当前用户已停用的技能名集合（只对自建技能生效）。

  Returns:
    每项 {name, title, description, category, domain, kind, owner, editable, enabled,
         overview, when_to_use, references}。
  """
  caps = discover_skills()
  refs_by_skill = _references_by_skill(caps)
  items: list[dict] = []
  for cap in main_capabilities(caps):
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
      'overview': _first_sentence(cap.description),
      'when_to_use': cap.when_to_use or _rest_sentences(cap.description),
      'references': refs_by_skill.get((owner, name), []),
    })
  return items
