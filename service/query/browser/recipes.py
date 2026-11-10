"""浏览器配方（声明式 DSL）—— 页面固定、参数可变。

一条配方 = 一条平台能力：页面路径 + 参数占位 + 步骤 + 就绪条件 + 抽取规则。
执行在旁路服务（`deploy/browser/app.py`），本模块只持有「页面知识」。

配方不能凭空写，必须先探查真实页面：
  1) `python -m deploy.scripts.browser_probe --system dcim --url <目标页> --dump`
     （或直接 curl 旁路服务 `POST /v1/inspect`）拿到可交互元素与表格结构；
  2) 按探针结果补齐 `steps` / `ready` / `extract` 的选择器，把 `enabled` 置 True；
  3) 重启后端，工具 `browser__<id>` 自动注册（`<available-tools>` 同步出现）。

`enabled=False` 的配方**不注册能力**（模板不会以"一调就错"的形态暴露给模型）。
选择器优先 `data-testid` / role+name / 稳定 class，禁用 nth-child 这类结构位置。
"""

from __future__ import annotations

from dataclasses import dataclass, field


class RecipeError(Exception):
  """配方/参数不合法（消息面向模型，可读）。"""


@dataclass(frozen=True)
class Recipe:
  """一条页面捕获配方。

  Attributes:
    id: 能力 id 后缀（'capture_dcim_alarms' → 工具名 `browser__capture_dcim_alarms`）。
    system: 登录态归属（dcim/mgmt），决定 storageState 文件与页面根地址。
    path: 拼在根地址后的页面路径，可含 `{参数}` 占位。
    enabled: 是否注册为能力。**选择器校准前保持 False**。
    params: 模型可传参数 {名: 说明}。
    param_enums: 固定取值参数 → 合法值（生成 schema enum）。
    defaults: 缺省值（不传时补）。
    steps: 动作序列（goto/click/fill/select/check/press/wait_for/wait_ms）。
    ready: 就绪条件 {selector, state, timeout_ms}。
    extract: 抽取规则 [{kind: text|table|html, selector, name?, max_chars?}]。
    login: 可选登录配方 {check, url, steps, ready, save_state}；账号密码由 client 从
      settings 注入 `{username}`/`{password}`。
    full_page: 整页截图（超长页面由旁路服务自动退回可视区）。
  """

  id: str
  name: str
  description: str
  system: str
  path: str
  enabled: bool = False
  params: dict[str, str] = field(default_factory=dict)
  param_enums: dict[str, tuple[str, ...]] = field(default_factory=dict)
  defaults: dict[str, str] = field(default_factory=dict)
  steps: tuple[dict, ...] = ()
  ready: dict = field(default_factory=dict)
  extract: tuple[dict, ...] = ()
  login: dict = field(default_factory=dict)
  full_page: bool = True


# ============================ 配方 ============================
# ⚠️ 下面两条是**模板**（enabled=False）：路径、选择器都需按真实页面校准。
#    校准前注册会以"一调就错"的形态暴露给模型，所以默认不注册。

RECIPES: dict[str, Recipe] = {
  'capture_dcim_alarms': Recipe(
    id='capture_dcim_alarms',
    name='DCIM 告警列表截图',
    description=(
      '进入动环监控（DCIM）的告警列表页，按园区/等级/时间范围筛选后截图，'
      '并返回页面上的告警表格与正文。用于「看一眼告警页面长什么样 / 截图给我」这类请求；'
      '要数据统计请用 dcim_meta 的查询能力，不要用截图替代。'
    ),
    system='dcim',
    path='/alarm/list',          # TODO 探针校准
    enabled=False,               # TODO 校准后置 True
    params={
      'park': '园区名称（如 廊坊 / 平湖 / 惠州）',
      'level': '告警等级',
      'hours': '最近多少小时（如 24）',
    },
    param_enums={'level': ('紧急', '重要', '次要', '提示')},
    defaults={'hours': '24'},
    steps=(
      # TODO 用 browser_probe 的 dump 结果替换以下选择器
      {'action': 'fill', 'selector': 'TODO-keyword-input', 'value': '{park}'},
      {'action': 'select', 'selector': 'TODO-level-select', 'value': '{level}'},
      {'action': 'fill', 'selector': 'TODO-hours-input', 'value': '{hours}'},
      {'action': 'click', 'selector': 'TODO-search-button'},
    ),
    ready={'selector': 'TODO-alarm-table', 'state': 'visible', 'timeout_ms': 20000},
    extract=(
      {'kind': 'table', 'selector': 'TODO-alarm-table', 'name': '告警列表'},
      {'kind': 'text', 'selector': 'body'},
    ),
  ),
  'capture_mgmt_center': Recipe(
    id='capture_mgmt_center',
    name='综合管理平台页面截图',
    description=(
      '进入综合管理平台（mgmt /center）的指定页面，按关键词筛选后截图并抽取页面表格与正文。'
      '用于「截图看一下这个页面」这类请求；要数据请用 mgmt 的查询能力。'
    ),
    system='mgmt',
    path='/center',              # TODO 探针校准
    enabled=False,               # TODO 校准后置 True
    params={
      'keyword': '页面筛选关键词（可留空）',
    },
    defaults={'keyword': ''},
    steps=(
      # TODO 用 browser_probe 的 dump 结果替换
      {'action': 'fill', 'selector': 'TODO-search-input', 'value': '{keyword}'},
      {'action': 'click', 'selector': 'TODO-search-button'},
    ),
    ready={'selector': 'body', 'state': 'visible', 'timeout_ms': 20000},
    extract=(
      {'kind': 'table', 'selector': 'TODO-main-table', 'name': '页面数据'},
      {'kind': 'text', 'selector': 'body'},
    ),
  ),
}


# ============================ 登录配方（交互浏览用） ============================
# 交互模式（BROWSER_EXPLORE_ENABLED）下 navigate 到某 system 时，若对该 system 登录态
# 失效，旁路服务会按这里的模板跑一次登录并回写 storageState。模板里的
# `{username}` / `{password}` 由 caller 用 BROWSER_<SYSTEM>_USERNAME/PASSWORD 替换。
#
# 同样是**模板**：`enabled=False` 时 caller 不发送 login（改为依赖事先用
# `deploy/scripts/browser_login.py` 落盘的 storageState）。验证码/SSO 场景必须走
# 手动登录引导，不要在这里硬编。
LOGIN_RECIPES: dict[str, dict] = {
  'dcim': {
    'enabled': False,                                   # TODO 探针校准后置 True
    'check': {'selector': 'TODO-logged-in-marker'},     # 命中即视为已登录
    'url': '',                                          # TODO 登录页地址；留空＝目标页自身跳登录
    'steps': [                                          # TODO 用 browser_probe 校准选择器
      {'action': 'fill', 'selector': 'TODO-username', 'value': '{username}'},
      {'action': 'fill', 'selector': 'TODO-password', 'value': '{password}'},
      {'action': 'click', 'selector': 'TODO-submit'},
    ],
    'ready': {'selector': 'TODO-main', 'state': 'visible', 'timeout_ms': 15000},
    'save_state': True,
  },
  'mgmt': {
    # 2026-11-03 实测校准（http://192.168.88.18/ 的 SPA 登录页）：手机号 + 密码 + 登录按钮。
    # 登录成功后跳 /#/index、侧边栏出现「人员管理」——用它作为"已登录"标记（登录页上出现 0 次）。
    'enabled': True,
    'check': {'selector': {'by': 'text', 'name': '人员管理', 'exact': True}},
    'url': '',
    'steps': [
      {'action': 'fill', 'selector': 'input[type="tel"]', 'value': '{username}'},
      {'action': 'fill', 'selector': 'input[type="password"]', 'value': '{password}'},
      {'action': 'click', 'selector': 'button.login-btn'},
    ],
    'ready': {
      'selector': {'by': 'text', 'name': '人员管理', 'exact': True},
      'state': 'visible', 'timeout_ms': 15000,
    },
    'save_state': True,
  },
}


def enabled_recipes() -> list[Recipe]:
  """已校准、可注册为能力的配方。"""
  return [r for r in RECIPES.values() if r.enabled]


def get_recipe(capability_id: str) -> Recipe | None:
  """'browser.capture_x' → Recipe；非本平台/未知返回 None。"""
  if not capability_id.startswith('browser.'):
    return None
  return RECIPES.get(capability_id.split('.', 1)[1])


def resolve_params(recipe: Recipe, params: dict | None) -> dict:
  """合并默认值并校验：只接受配方声明的参数，枚举参数必须在合法值内。

  返回**只含声明键**的 dict（多余的模型参数直接拒绝，避免悄悄拼进 URL/选择器）。
  """
  incoming = {k: v for k, v in (params or {}).items() if v not in ('', None)}
  extra = set(incoming) - set(recipe.params)
  if extra:
    raise RecipeError(f'不支持的参数: {", ".join(sorted(extra))}')
  merged = {k: str(v) for k, v in recipe.defaults.items()}
  merged.update({k: str(v) for k, v in incoming.items()})
  for name, allowed in recipe.param_enums.items():
    value = merged.get(name)
    if value is not None and value not in allowed:
      raise RecipeError(f'参数 {name} 取值非法: {value}（可选：{" / ".join(allowed)}）')
  return merged
