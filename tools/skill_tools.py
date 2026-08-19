"""skill_manage 工具 — 用户通过对话创建/查看/修改自己的技能。

复刻 DeerFlow skill_manage_tool.py 精简改造，适配我们的全局单例 Agent：
- 全部技能进全局 skill 平台注册，id 全局唯一（创建时全局查重，同名直接拒绝）。
- 按用户隔离：user_id 取 core/agent/user_context.get_current_user_id()（Agent
  stream 入口设置）；每个动作只操作当前用户自己名下的技能，越权报「无权限」。
- 动作：create / edit / patch / write_file / remove_file / delete / list / read /
  rollback。写盘前静态扫描（CRITICAL 拒绝）+ LLM 语义扫描（fail-closed，可执行
  脚本必须 allow），写盘后重跑 register_skills_platform() + bump_skills_version()
  触发下一条消息的 agent 重建，新技能立即可用（对齐 deer-flow「下个会话可见」）。
- list/read 为只读动作，不触发重建。
"""

import asyncio
import json
import logging
from typing import Literal
from weakref import WeakValueDictionary

from langchain.tools import tool
from langchain_core.runnables.config import RunnableConfig

from core.agent.user_context import get_current_user_id
from service.query.skills.storage import (
  append_history,
  bump_skills_version,
  delete_skill,
  ensure_user_skill_editable,
  list_user_skills,
  read_skill,
  remove_skill_file,
  resolve_skill_file,
  rollback_skill,
  scan_llm_skill,
  scan_static_skill,
  skill_exists_globally,
  validate_skill_markdown_content,
  validate_skill_name,
  write_skill,
)

logger = logging.getLogger(__name__)

# 锁粒度：(user_id, skill_name)，避免跨用户互相阻塞
_skill_locks: WeakValueDictionary[tuple[str, str], asyncio.Lock] = WeakValueDictionary()


def _get_lock(user_id: str, name: str) -> asyncio.Lock:
  key = (user_id, name)
  lock = _skill_locks.get(key)
  if lock is None:
    lock = asyncio.Lock()
    _skill_locks[key] = lock
  return lock


def _history_record(*, action: str, file_path: str, prev_content: str | None,
                    new_content: str | None, thread_id: str | None, scanner: dict) -> dict:
  return {
    'action': action,
    'author': 'agent',
    'thread_id': thread_id,
    'file_path': file_path,
    'prev_content': prev_content,
    'new_content': new_content,
    'scanner': scanner,
  }


def _format_findings(findings: list[dict]) -> str:
  parts = []
  for f in findings:
    loc = f.get('file') or '<unknown>'
    if f.get('line') is not None:
      loc = f'{loc}:{f["line"]}'
    parts.append(f"{f.get('rule_id')} ({f.get('severity')}) at {loc}: {f.get('message')} Evidence: {f.get('evidence') or '<none>'}")
  return '; '.join(parts)


async def _scan_or_raise(name: str, rel_path: str, content: str, *, executable: bool) -> dict:
  """静态扫描（CRITICAL 拒绝）+ LLM 语义扫描（fail-closed）后返回扫描结果。

  可执行脚本（scripts/ 下）只有显式 allow 才放行，warn 也拒绝。
  """
  static_findings = scan_static_skill(name, rel_path, content)
  blocked = [f for f in static_findings if f['severity'] == 'CRITICAL']
  if blocked:
    raise ValueError(f'静态安全扫描拒绝写入：{_format_findings(blocked)}')
  result = await scan_llm_skill(
    content, executable=executable, location=f'{name}/{rel_path}', static_findings=static_findings,
  )
  if result['decision'] == 'block':
    raise ValueError(f'安全扫描拒绝写入：{result["reason"]}')
  if executable and result['decision'] != 'allow':
    raise ValueError(f'安全扫描拒绝可执行脚本：{result["reason"]}')
  return result


def _reload_skills() -> None:
  """写盘后刷新全局技能注册 + 检索索引，并 bump 版本号（下条消息触发 agent 重建）。"""
  from service.query.base.platform import register_platform
  from service.query.skills.platform import register_skills_platform
  register_platform(register_skills_platform())
  bump_skills_version()


def _require_name(user_id: str, name: str) -> str:
  """校验并返回规范化技能名；同时确认属于当前用户。"""
  normalized = validate_skill_name(name)
  ensure_user_skill_editable(user_id, normalized)
  return normalized


def _thread_id(config: RunnableConfig | None) -> str | None:
  if not config:
    return None
  return (config.get('configurable') or {}).get('thread_id')


@tool
async def skill_manage(
  action: Literal['create', 'edit', 'patch', 'write_file', 'remove_file', 'delete', 'list', 'read', 'rollback'],
  name: str = '',
  content: str | None = None,
  path: str | None = None,
  find: str | None = None,
  replace: str | None = None,
  expected_count: int | None = None,
  index: int | None = None,
  config: RunnableConfig = None,
) -> str:
  """创建、查看、修改当前用户自己的技能（按用户隔离，只能操作本人技能）。

  技能 = 一个目录，含 SKILL.md（frontmatter name/title/description/category/
  domain/kind + 正文指导如何执行）+ 可选 references/templates/scripts/assets 支持文件。
  创建后立即生效（下一条对话消息可检索调用）。所有写盘都过安全扫描，
  静态规则命中（如路径穿越/动态执行/云元数据/私钥）或 LLM 扫描 block 时拒绝。

  Args:
    action: 动作。
      - create：新建技能，content 为完整 SKILL.md，技能名全局唯一（公开技能与
        其他用户的技能都不可重名）；
      - edit：整体重写本人技能的 SKILL.md，content 为新完整内容；
      - patch：精准替换 SKILL.md 中一段文本，find/replace 必填，expected_count
        可选（期望匹配次数，不符报错）；
      - write_file：写支持文件（references/templates/scripts/assets 下），
        path 为相对路径，content 为文件内容；
      - remove_file：删除支持文件，path 为相对路径；
      - delete：删除本人整个技能；
      - list：列出本人全部技能（name/title/description）；
      - read：读取本人技能 SKILL.md 全文；
      - rollback：回滚到历史第 index 条记录的状态（index 从 0 起，旧→新）。
    name: 技能名（小写字母/数字/下划线，≤64 字符）；list 动作可不填。
    content: create/edit/write_file 时必填的完整文件内容。
    path: write_file/remove_file 的目标相对路径，如 references/guide.md。
    find: patch 时待替换的原文（需精确匹配）。
    replace: patch 时的替换文本。
    expected_count: patch 可选，期望替换次数。
    index: rollback 的历史记录下标。
  """
  user_id = get_current_user_id()
  if not user_id:
    return json.dumps({'error': '无法识别当前用户身份，技能管理仅对登录用户开放'}, ensure_ascii=False)

  if action == 'list':
    return json.dumps({'skills': list_user_skills(user_id)}, ensure_ascii=False)

  normalized = validate_skill_name(name)
  lock = _get_lock(user_id, normalized)
  tid = _thread_id(config)
  async with lock:
    try:
      if action == 'create':
        if skill_exists_globally(normalized):
          raise ValueError(f'技能名「{normalized}」已存在（公开技能或其他用户已占用），请换一个名字')
        if not content:
          raise ValueError('create 需要 content（完整 SKILL.md）')
        validate_skill_markdown_content(normalized, content)
        scan = await _scan_or_raise(normalized, 'SKILL.md', content, executable=False)
        write_skill(user_id, normalized, content)
        append_history(user_id, normalized, _history_record(
          action='create', file_path='SKILL.md', prev_content=None, new_content=content,
          thread_id=tid, scanner=scan,
        ))
        _reload_skills()
        return f'技能「{normalized}」已创建。告诉用户它已可用，可在下一条对话中按需调用。'

      if action == 'edit':
        normalized = _require_name(user_id, normalized)
        if not content:
          raise ValueError('edit 需要 content（新的完整 SKILL.md）')
        validate_skill_markdown_content(normalized, content)
        prev = read_skill(user_id, normalized)
        scan = await _scan_or_raise(normalized, 'SKILL.md', content, executable=False)
        write_skill(user_id, normalized, content)
        append_history(user_id, normalized, _history_record(
          action='edit', file_path='SKILL.md', prev_content=prev, new_content=content,
          thread_id=tid, scanner=scan,
        ))
        _reload_skills()
        return f'技能「{normalized}」已更新。'

      if action == 'patch':
        normalized = _require_name(user_id, normalized)
        if find is None or replace is None:
          raise ValueError('patch 需要 find 和 replace')
        prev = read_skill(user_id, normalized)
        occurrences = prev.count(find)
        if occurrences == 0:
          raise ValueError('patch 目标文本未在 SKILL.md 中找到')
        if expected_count is not None and occurrences != expected_count:
          raise ValueError(f'期望替换 {expected_count} 次，实际匹配 {occurrences} 次')
        count = expected_count if expected_count is not None else 1
        new_content = prev.replace(find, replace, count)
        validate_skill_markdown_content(normalized, new_content)
        scan = await _scan_or_raise(normalized, 'SKILL.md', new_content, executable=False)
        write_skill(user_id, normalized, new_content)
        append_history(user_id, normalized, _history_record(
          action='patch', file_path='SKILL.md', prev_content=prev, new_content=new_content,
          thread_id=tid, scanner=scan,
        ))
        _reload_skills()
        return f'技能「{normalized}」已打补丁（匹配 {occurrences} 处，替换 {count} 处）。'

      if action == 'write_file':
        normalized = _require_name(user_id, normalized)
        if not path or content is None:
          raise ValueError('write_file 需要 path 和 content')
        target = resolve_skill_file(user_id, normalized, path)
        exists = target.is_file()
        prev_content = target.read_text(encoding='utf-8', errors='replace') if exists else None
        executable = path.startswith('scripts/')
        scan = await _scan_or_raise(normalized, path, content, executable=executable)
        write_skill(user_id, normalized, content, path)
        append_history(user_id, normalized, _history_record(
          action='write_file', file_path=path, prev_content=prev_content, new_content=content,
          thread_id=tid, scanner=scan,
        ))
        _reload_skills()
        return f'文件「{path}」已写入技能「{normalized}」。'

      if action == 'remove_file':
        normalized = _require_name(user_id, normalized)
        if not path:
          raise ValueError('remove_file 需要 path')
        prev_content = remove_skill_file(user_id, normalized, path)
        append_history(user_id, normalized, _history_record(
          action='remove_file', file_path=path, prev_content=prev_content, new_content=None,
          thread_id=tid, scanner={'decision': 'allow', 'reason': 'Deletion requested.'},
        ))
        _reload_skills()
        return f'文件「{path}」已从技能「{normalized}」移除。'

      if action == 'delete':
        normalized = _require_name(user_id, normalized)
        prev_content = read_skill(user_id, normalized)
        delete_skill(user_id, normalized)
        append_history(user_id, normalized, _history_record(
          action='delete', file_path='SKILL.md', prev_content=prev_content, new_content=None,
          thread_id=tid, scanner={'decision': 'allow', 'reason': 'Deletion requested.'},
        ))
        _reload_skills()
        return f'技能「{normalized}」已删除。'

      if action == 'read':
        normalized = _require_name(user_id, normalized)
        return json.dumps({'name': normalized, 'content': read_skill(user_id, normalized)}, ensure_ascii=False)

      if action == 'rollback':
        normalized = _require_name(user_id, normalized)
        if index is None:
          raise ValueError('rollback 需要 index（历史记录下标，从 0 起）')
        message = rollback_skill(user_id, normalized, index)
        _reload_skills()
        return message

      raise ValueError(f'不支持的动作：{action}')
    except ValueError as e:
      return json.dumps({'error': str(e)}, ensure_ascii=False)
