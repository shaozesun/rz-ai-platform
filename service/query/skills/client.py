"""skills 能力调用 —— 按 capability_id 段数分流读取 skill 文档（纯本地）。

- 主能力 `skill.<name>`（2 段）：返回 SKILL.md 全文 + references 索引清单。
- 子能力 `skill.<name>.<stem>`（3 段）：返回 references/<stem>.md 全文，
  与旧 chart client 同构（{'type': stem, 'spec': <全文>}）。

skill 名 / stem 白名单校验（^[a-z0-9_]+$），防路径穿越。
"""

import logging
import re
from pathlib import Path

from service.query.skills.discover import SKILLS_DIR
from service.query.skills.frontmatter import split_skill_markdown

logger = logging.getLogger(__name__)

_SAFE_ID = re.compile(r'^[a-z0-9_]+$')


class SkillsError(Exception):
  """技能能力调用异常。"""


def _parse_id(capability_id: str) -> list[str]:
  """校验能力 id 并返回 skill 名后的段列表（如 skill.chart.line → ['chart', 'line']）。"""
  if not capability_id.startswith('skill.'):
    raise SkillsError(f'非技能能力 {capability_id}（应为 skill.<name> 或 skill.<name>.<stem>）')
  parts = capability_id.split('.')[1:]
  if not 1 <= len(parts) <= 2 or not all(_SAFE_ID.match(p) for p in parts):
    raise SkillsError(f'非法能力 id {capability_id}')
  return parts


def _references_index(skill_dir: Path) -> list[dict]:
  """列出 skill 目录下 references 索引（type/name/description），供主能力返回。"""
  refs_dir = skill_dir / 'references'
  if not refs_dir.is_dir():
    return []
  index: list[dict] = []
  for ref_file in sorted(refs_dir.glob('*.md')):
    stem = ref_file.stem
    if not _SAFE_ID.match(stem):
      continue
    try:
      content = ref_file.read_text(encoding='utf-8')
    except OSError:
      continue
    parsed = split_skill_markdown(content)
    name = stem
    desc = ''
    if parsed is not None:
      name = str(parsed[0].get('name') or stem)
      desc = str(parsed[0].get('description') or '')
    index.append({'type': stem, 'name': name, 'description': desc})
  return index


def resolve_skill_dir(name: str) -> tuple[Path, str] | None:
  """按名定位技能目录，返回 (目录, owner)；不存在返回 None。

  先查公开 skills/<name>/（owner=''），再扫 skills/users/<uid>/<name>/（owner=uid，
  取排序后首个匹配）。技能名全局唯一（创建时查重），正常不会公开/用户重名。
  """
  public = SKILLS_DIR / name
  if public.is_dir() and (public / 'SKILL.md').is_file():
    return public, ''
  users_root = SKILLS_DIR / 'users'
  if users_root.is_dir():
    for user_dir in sorted(users_root.iterdir()):
      if not user_dir.is_dir() or not _SAFE_ID.match(user_dir.name):
        continue
      candidate = user_dir / name
      if candidate.is_dir() and (candidate / 'SKILL.md').is_file():
        return candidate, user_dir.name
  return None


def _check_owner(owner: str, on_behalf_of: str | None) -> None:
  """越权访问（技能归属他人）统一报「不存在或无权限」，不暴露归属。"""
  if owner and owner != (on_behalf_of or ''):
    raise SkillsError('技能不存在或无权限')


def build_caller():
  """构造 skills 平台的 caller（符合 Platform.caller 契约）。"""

  async def call(
    capability_id: str,
    params: dict | None = None,
    *,
    on_behalf_of: str | None = None,
  ) -> dict:
    parts = _parse_id(capability_id)
    name = parts[0]

    resolved = resolve_skill_dir(name)
    if resolved is None:
      raise SkillsError(f'技能 {name} 不存在')
    skill_dir, owner = resolved
    _check_owner(owner, on_behalf_of)

    if len(parts) == 1:
      # 主能力：返回 SKILL.md 全文 + references 索引
      skill_file = skill_dir / 'SKILL.md'
      try:
        skill_md = skill_file.read_text(encoding='utf-8')
      except OSError as e:
        raise SkillsError(f'读取技能失败 {skill_file.name}: {e}') from e
      logger.info('skills.call main id=%s', capability_id)
      return {
        'skill': name,
        'skill_md': skill_md,
        'references': _references_index(skill_dir),
      }

    # 子能力：返回 references/<stem>.md 全文
    stem = parts[1]
    ref_file = skill_dir / 'references' / f'{stem}.md'
    if not ref_file.is_file():
      raise SkillsError(f'技能 {name} 无子能力 {stem}（缺 {ref_file.name}）')
    try:
      spec = ref_file.read_text(encoding='utf-8')
    except OSError as e:
      raise SkillsError(f'读取子能力规范失败 {ref_file.name}: {e}') from e
    logger.info('skills.call sub id=%s', capability_id)
    return {'type': stem, 'spec': spec}

  return call
