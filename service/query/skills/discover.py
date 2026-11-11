"""skills/ 目录自动发现 —— 声明式生成 Capability。

- skills/<name>/SKILL.md → 主能力 `skill.<name>`（description 来自 frontmatter）
- skills/<name>/references/<stem>.md → 子能力 `skill.<name>.<stem>`（description
  来自 references 的 frontmatter，缺省 fallback 到首行标题/stem）

存储布局（用户自建技能按用户隔离）：
- 公开技能  skills/<name>/                 → owner=''（所有用户可见）
- 用户技能  skills/users/<safe_user_id>/<name>/ → owner=user_id（仅本人可见）

零代码接入新 skill：丢一个目录即自动进 tool_search 索引与 <available-tools>
（公开技能）；用户技能进索引但带 owner 标记，tool_search/caller 按运行期用户
隔离。扫描按 skill 名 + stem 排序，保证 catalog hash 确定性。

运行时取数走 `discover_skills_cached()`（进程内快照）：启动时扫一次，写盘/删除经
`reload_skills()` 刷新，其它 worker 重启后跟上。**不要按请求直接扫盘**。
"""

import logging
import re
from pathlib import Path

from service.query.base.capability import Capability
from service.query.skills.frontmatter import split_skill_markdown

logger = logging.getLogger(__name__)

SKILLS_DIR = Path(__file__).resolve().parents[3] / 'skills'
_USERS_DIR = SKILLS_DIR / 'users'

# skill 名 / references stem / user_id 白名单，防路径穿越与非法 id
_SAFE_ID = re.compile(r'^[a-z0-9_]+$')


def _reference_meta(content: str, stem: str) -> tuple[str, str]:
  """取 references md 的 (name, description)：优先 frontmatter，缺省用 stem/首行标题。"""
  parsed = split_skill_markdown(content)
  if parsed is not None:
    meta = parsed[0]
    name = str(meta.get('name') or stem)
    desc = str(meta.get('description') or '')
    if desc:
      return name, desc
  first_line = content.splitlines()[0].strip() if content.splitlines() else ''
  return stem, first_line.lstrip('# ').strip() or stem


def _scan_skill_dir(skill_dir: Path, owner: str) -> list[Capability]:
  """扫描单个技能目录，生成主能力 + 子能力（带 owner）。

  Returns:
    Capability 列表；目录无合法 SKILL.md 时返回空列表。
  """
  caps: list[Capability] = []
  name = skill_dir.name
  if not _SAFE_ID.match(name):
    logger.warning('跳过 skill %s：名称含非法字符', name)
    return caps

  skill_file = skill_dir / 'SKILL.md'
  if not skill_file.is_file():
    logger.warning('跳过 skill %s：缺少 SKILL.md', name)
    return caps
  try:
    content = skill_file.read_text(encoding='utf-8')
  except (OSError, UnicodeDecodeError) as e:
    logger.warning('跳过 skill %s：读取失败 %s', name, e)
    return caps

  parsed = split_skill_markdown(content)
  if parsed is None:
    logger.warning('跳过 skill %s：SKILL.md 无有效 frontmatter', name)
    return caps
  metadata, _ = parsed
  category = str(metadata.get('category') or '')
  domain = str(metadata.get('domain') or category)
  caps.append(Capability(
    id=f'skill.{name}',
    name=str(metadata.get('title') or name),
    description=str(metadata.get('description') or ''),
    params={},
    category=category,
    domain=domain,
    kind=str(metadata.get('kind') or '资源'),
    owner=owner,
    when_to_use=str(metadata.get('when_to_use') or ''),
  ))

  refs_dir = skill_dir / 'references'
  if not refs_dir.is_dir():
    return caps
  try:
    ref_files = sorted(refs_dir.glob('*.md'))
  except OSError as e:
    logger.warning('跳过子能力 %s：references 目录不可读 %s', name, e)
    return caps
  for ref_file in ref_files:
    stem = ref_file.stem
    if not _SAFE_ID.match(stem):
      logger.warning('跳过子能力 %s/%s：名称含非法字符', name, stem)
      continue
    try:
      ref_content = ref_file.read_text(encoding='utf-8')
    except (OSError, UnicodeDecodeError) as e:
      logger.warning('跳过子能力 %s/%s：读取失败 %s', name, stem, e)
      continue
    ref_name, ref_desc = _reference_meta(ref_content, stem)
    caps.append(Capability(
      id=f'skill.{name}.{stem}',
      name=ref_name,
      description=ref_desc,
      params={},
      category=category,
      domain=domain,
      kind='资源',
      owner=owner,
    ))
  return caps


def _iter_skill_dirs(path: Path, label: str) -> list[Path]:
  """列出子目录（不可读时返回空列表 —— 一层目录失败不能炸掉整张技能表）。"""
  try:
    return sorted(p for p in path.iterdir() if p.is_dir())
  except OSError as e:
    logger.warning('跳过 %s：目录不可读 %s', label, e)
    return []


def discover_skills() -> list[Capability]:
  """扫描 skills/ 目录生成全部 Capability（公开 + 用户，按用户隔离）。

  公开扫描跳过 users/ 目录；users/<uid>/ 下每个技能目录按 owner=uid 扫描，
  uid 同样过 _SAFE_ID 白名单。
  """
  caps: list[Capability] = []
  if not SKILLS_DIR.is_dir():
    logger.warning('skills 目录不存在: %s', SKILLS_DIR)
    return caps

  for skill_dir in _iter_skill_dirs(SKILLS_DIR, 'skills 目录'):
    if skill_dir.name == 'users':
      continue  # 用户技能单独扫描（带 owner）
    caps.extend(_scan_skill_dir(skill_dir, owner=''))

  if _USERS_DIR.is_dir():
    for user_dir in _iter_skill_dirs(_USERS_DIR, 'skills/users 目录'):
      uid = user_dir.name
      if not _SAFE_ID.match(uid):
        logger.warning('跳过用户技能目录 %s：用户 id 含非法字符', uid)
        continue
      for skill_dir in _iter_skill_dirs(user_dir, f'用户技能目录 {uid}'):
        if skill_dir.name.startswith('.'):
          continue  # .history 等隐藏目录不是技能
        caps.extend(_scan_skill_dir(skill_dir, owner=uid))

  logger.info('skills 自动发现完成 count=%d', len(caps))
  return caps


# 进程内技能快照：启动时扫一次（`main.py` lifespan），写盘/删除经 `reload_skills()`
# 刷新。列表与工具目录都读它，**不按请求扫盘** —— 实时扫描会把目录抖动放大成整表 500。
_SNAPSHOT: list[Capability] | None = None


def refresh_skills_cache() -> int:
  """重新扫盘并替换快照；扫描失败保留旧快照（不抛异常，列表不能因此变空）。"""
  global _SNAPSHOT
  try:
    caps = discover_skills()
  except Exception as e:  # noqa: BLE001 —— 目录/编码/权限异常都不应炸掉列表
    logger.error('skills 快照刷新失败，沿用上一份（%d 条）: %s', len(_SNAPSHOT or []), e)
    return len(_SNAPSHOT or [])
  _SNAPSHOT = caps
  return len(caps)


def discover_skills_cached() -> list[Capability]:
  """返回进程内快照（首次调用时懒构建）。"""
  if _SNAPSHOT is None:
    refresh_skills_cache()
  return list(_SNAPSHOT or [])
