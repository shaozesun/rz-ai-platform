"""SKILL.md frontmatter 解析 —— 复刻 DeerFlow skills/frontmatter.py 的
split_skill_markdown()：正则拆 `---` 包裹的 YAML 头 + `yaml.safe_load`。

解析失败（无 frontmatter / YAML 错误 / 非 dict）返回 None，由调用方决定
跳过该 skill 并告警，不阻断其它 skill 的发现。
"""

import re

import yaml

_FRONTMATTER_RE = re.compile(r'^---\s*\n(.*?)\n---\s*\n?', re.DOTALL)


def split_skill_markdown(content: str) -> tuple[dict, str] | None:
  """把 SKILL.md 拆成 frontmatter 元数据与正文。

  Returns:
    (metadata, body)；无 frontmatter / 非 dict / YAML 错误时返回 None。
  """
  match = _FRONTMATTER_RE.match(content)
  if not match:
    return None
  try:
    metadata = yaml.safe_load(match.group(1))
  except yaml.YAMLError:
    return None
  if not isinstance(metadata, dict):
    return None
  return {str(k): v for k, v in metadata.items()}, content[match.end():]
