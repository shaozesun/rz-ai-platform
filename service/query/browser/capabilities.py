"""browser 平台能力声明 —— 由已校准的配方动态生成。

每个 `Recipe(enabled=True)` → 一条 `browser.<recipe.id>` 能力；未校准的模板不注册
（避免以"一调就错"的形态暴露给模型）。新增页面只改 `recipes.py`，本文件不用动。
"""

from __future__ import annotations

from service.query.base.capability import Capability
from service.query.browser.recipes import Recipe, enabled_recipes


def _description(recipe: Recipe) -> str:
  return (
    f'{recipe.description}'
    '截图是页面原样（可能含敏感信息，仅本会话产物可见）。'
    '工具返回 artifacts（截图 url/name）、页面标题、正文摘要与表格；'
    '要给用户看图时，用一行 <file url="…" name="…"/> 输出截图。'
    '本能力用于"看页面/截图"，精确数据仍应使用对应平台的数据查询能力。'
  )


def build_capabilities() -> tuple[Capability, ...]:
  caps: list[Capability] = []
  for recipe in enabled_recipes():
    caps.append(Capability(
      id=f'browser.{recipe.id}',
      name=recipe.name,
      description=_description(recipe),
      params=dict(recipe.params),
      param_enums=dict(recipe.param_enums),
      category='page',
      domain='capture',
      kind='动作',
    ))
  return tuple(caps)
