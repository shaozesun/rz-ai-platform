"""browser 平台注册 —— 页面截图能力（Playwright 旁路容器）。

两个开关（都默认关）：
- `BROWSER_ENABLED`：声明式配方能力（页面/筛选固定）。
- `BROWSER_EXPLORE_ENABLED`：7 个交互浏览工具（navigate/snapshot/click/get_text/
  screenshot/record/close），模型逐步操作页面；record 在当前页面录固定时长，
  交付关键帧 + 可选 webm。
"""

from __future__ import annotations

from config.settings import settings
from service.query.base.platform import Platform
from service.query.browser import client, interactive
from service.query.browser.capabilities import build_capabilities


async def _call(capability_id: str, params: dict, *, on_behalf_of: str | None = None) -> dict:
  """配方能力走 client，交互能力走 interactive（两边开关各自校验）。"""
  if interactive.handles(capability_id):
    return await interactive.call(capability_id, params, on_behalf_of=on_behalf_of)
  return await client.call(capability_id, params, on_behalf_of=on_behalf_of)


def browser_platform() -> Platform:
  caps = list(build_capabilities())
  if settings.BROWSER_EXPLORE_ENABLED:
    caps.extend(interactive.build_interactive_capabilities())
  return Platform(name='browser', capabilities=tuple(caps), caller=_call)
