"""Agent 服务 — 单例管理 Agent 图实例"""

import logging
from typing import AsyncIterator

from core.agent.build import build_rz_agent
from core.agent.middlewares.guardrail import set_current_permissions
from service.agent.streaming import stream_agent

logger = logging.getLogger(__name__)


class AgentService:
  """Agent 服务单例。

  持有编译后的 LangGraph 图，提供流式对话接口。
  """

  _instance: 'AgentService | None' = None

  def __new__(cls):
    if cls._instance is None:
      cls._instance = super().__new__(cls)
      cls._instance._initialized = False
    return cls._instance

  def __init__(self):
    if self._initialized:
      return
    self._initialized = True
    self._agent = None
    self._model = None
    self._catalog_hash = None

  def _ensure_agent(self):
    """确保 Agent 已初始化（model 变更时重建；权限不影响构建，见 build_rz_agent 说明）"""
    from core.model_gateway import model_gateway
    from tools.mock_tools import get_current_time
    from tools.mock_ops_tools import send_notification
    from tools.rag_tools import rag_knowledge_search
    from config.settings import settings

    # 每次检查 model 是否需要更新（配置热加载场景）
    model = model_gateway.as_langchain_model()

    # 注意（P0-3）：query_device_info / query_maintenance_history / query_duty_roster /
    # create_work_order / query_work_orders / query_attendance_records 已从常驻工具
    # 列表移除 —— 它们与 mgmt/dcim 平台真实能力功能重叠，且常驻工具对 LLM 始终可见，
    # 而平台工具走 deferred（默认隐藏，需 tool_search 才可见），导致 LLM 永远优先调
    # mock、真实平台永不被调用。这些函数定义本身保留在 tools/mock_ops_tools.py 供
    # 测试参考，仅不再注册进 Agent。send_notification 暂无对应平台能力，保留注册。
    tools = [
      rag_knowledge_search,
      get_current_time,
      send_notification,
    ]

    # 平台 deferred 工具：任一 <PLATFORM>_ENABLED 开关开启时，把已注册平台的能力
    # 生成为真工具（带 schema 强校验），交给 deferred filter 隐藏 schema，LLM 经
    # tool_search（语义检索）按需加载。
    # 注意：闸门是「是否有平台被启用」，不能只看 DCIM_ENABLED —— 否则只开
    # MGMT_ENABLED/SECURITY_ENABLED 时平台完全不会注册（历史 bug，P0-1）。
    middleware = []
    extra_prompt = ''
    from service.query.bootstrap import register_enabled_platforms

    platform_names = register_enabled_platforms()
    if platform_names:
      from service.query.base.tools_factory import build_all_tools
      from core.agent.deferred import build_deferred_setup, deferred_tools_prompt

      platform_tools = build_all_tools()
      deferred_tools, deferred_mw, deferred_names, catalog_hash = build_deferred_setup(
        platform_tools, top_k=settings.DCIM_RETRIEVAL_TOP_K,
      )
      # deferred 工具 + tool_search 是 async / 返回 Command，走 raw 通道不包装
      raw_tools = deferred_tools
      middleware.append(deferred_mw)
      extra_prompt = deferred_tools_prompt(deferred_names)
      self._catalog_hash = catalog_hash
    else:
      raw_tools = []
      self._catalog_hash = None

    # 首次构建或 model 变更时重建（权限不触发重建：RBAC 走 stream() 里
    # set_current_permissions 设置的运行时 contextvar 检查，见 build.py 说明）
    if self._agent is None or self._model is not model:
      self._model = model
      self._agent = build_rz_agent(
        model, tools,
        middleware=middleware, extra_prompt=extra_prompt, raw_tools=raw_tools,
      )
      logger.info('AgentService: Agent 已初始化')

  async def stream(
    self,
    message: str,
    thread_id: str,
    user_id: str,
    session_id: str,
    permissions: set[str] | None = None,
  ) -> AsyncIterator[str]:
    """Agent 流式对话。

    Args:
      message: 用户消息
      thread_id: LangGraph thread_id（用于 checkpoint）
      user_id: 用户 ID（透传给审计日志）
      session_id: 前端会话 ID
      permissions: 用户权限集合，用于 RBAC 工具过滤

    Yields:
      SSE 格式的字符串
    """
    perms = permissions or set()

    # 设置当前请求的 RBAC 上下文（工具调用时按此运行时检查）
    set_current_permissions(perms)

    # 确保 Agent 初始化
    self._ensure_agent()

    # 多标签编排 Step1+2：意图识别 → 查绑定表 → 预 promote 工具组。
    # 无预 promote（detect/空标签/无平台启用）时为空 dict，agent 正常走 tool_search。
    # P1-1：intent_labels 词汇表未定义时 build_intent_bindings() 恒为空，
    # route_to_tools 必然返回 None，此时 plan_query 的结构化 LLM 调用产出会被
    # 100% 丢弃——短路跳过，省一次白付的 LLM 调用。能力集稳定后在 Capability 上
    # 填 intent_labels，词汇表非空时会自动恢复走 Step1+2 快路径。
    initial_state = {}
    if self._catalog_hash:
      from service.query.base.intent_bindings import all_intent_labels
      if all_intent_labels():
        from core.agent.orchestrator import build_initial_state
        initial_state = await build_initial_state(message, self._catalog_hash)

    async for sse_chunk in stream_agent(
      agent=self._agent,
      message=message,
      thread_id=thread_id,
      user_id=user_id,
      session_id=session_id,
      initial_state=initial_state,
    ):
      yield sse_chunk
