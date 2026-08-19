"""Agent 服务 — 单例管理 Agent 图实例 + Checkpointer 生命周期"""

import asyncio
import logging
from typing import AsyncIterator

from core.agent.build import build_rz_agent
from core.agent.middlewares.guardrail import set_current_permissions
from service.agent.streaming import stream_agent

logger = logging.getLogger(__name__)


class AgentService:
  """Agent 服务单例。

  持有编译后的 LangGraph 图，提供流式对话接口。
  管理 Checkpointer（MemorySaver / AsyncPostgresSaver）生命周期。
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
    self._checkpointer = None
    self._checkpointer_closer = None
    self._fingerprint = None
    self._build_lock = None

  async def _ensure_agent(self):
    """确保 Agent 已初始化：按「图构建指纹」判断是否重建。

    历史 bug：重建判据曾是 `self._model is not model`，而 model_gateway.as_langchain_model()
    每请求都返回新的 ChatOpenAI 对象（无缓存），判据恒真 → 每请求重建 agent + checkpointer。
    后果：memory 后端 MemorySaver 每请求新建、多轮对话历史全丢；postgres 后端连接池每请求
    新建且旧 closer 被覆盖永不关闭 → 连接泄漏。现改为配置指纹比较，仅模型配置/工具开关/
    checkpointer 后端变化才重建（保留配置热加载语义，对齐 deerflow「checkpointer 常驻复用」）。
    """
    from config.settings import settings

    fingerprint = self._build_fingerprint(settings)
    if self._agent is not None and self._fingerprint == fingerprint:
      return

    # 懒建锁：避免 fork 后继承已被锁定的 asyncio.Lock
    if self._build_lock is None:
      self._build_lock = asyncio.Lock()
    async with self._build_lock:
      # double-checked：等锁期间可能已被其它并发首请求构建
      if self._agent is not None and self._fingerprint == fingerprint:
        return
      # 真重建：先关旧 checkpointer（防连接池泄漏），再构建新的
      if self._checkpointer_closer is not None:
        await self._checkpointer_closer()
        self._checkpointer_closer = None
      await self._rebuild(settings)
      self._fingerprint = fingerprint
      logger.info('AgentService: Agent 已初始化 fingerprint=%s', fingerprint)

  @staticmethod
  def _build_fingerprint(settings) -> tuple:
    """图构建指纹：影响 agent 结构/checkpointer 的配置集合，任一变化触发重建。

    含 get_skills_version()：skill_manage 写盘后版本 +1，下一条消息触发 agent
    重建，新技能立即可用（热加载）。
    """
    from service.query.skills.storage import get_skills_version
    return (
      settings.LLM_MODEL,
      settings.LLM_TEMPERATURE,
      settings.AGENT_LLM_TIMEOUT,
      settings.AGENT_LLM_MAX_RETRIES,
      settings.AGENT_ANALYSIS_ENABLED,
      settings.DCIM_ENABLED,
      settings.MGMT_ENABLED,
      settings.SECURITY_ENABLED,
      settings.AGENT_CHECKPOINT_BACKEND,
      get_skills_version(),
    )

  async def _rebuild(self, settings):
    """（仅真重建时执行）装配工具/middleware、创建 checkpointer、编译 LangGraph 图。"""
    from core.model_gateway import model_gateway
    from core.agent.middlewares.tool_error import wrap_async_tool_with_error_handling
    from tools.mock_tools import get_current_time
    from tools.mock_ops_tools import send_notification
    from tools.rag_tools import rag_knowledge_search
    from tools.skill_tools import skill_manage

    model = model_gateway.as_langchain_model()
    self._model = model

    tools = [
      rag_knowledge_search,
      get_current_time,
      send_notification,
    ]

    # 数据分析沙箱工具（async，走 raw_tools）：全量数据物化 + 沙箱 pandas 分析。
    # 用 async 错误包装套一层——工具异常转错误串喂回 LLM（ReAct 自我纠正），
    # 不中断整轮流（同步 wrapper 包 async 工具会丢 coroutine，见 tool_error.py）。
    analysis_tools = []
    if settings.AGENT_ANALYSIS_ENABLED:
      from tools.analysis_tools import build_analysis_tools
      analysis_tools = [
        wrap_async_tool_with_error_handling(t) for t in build_analysis_tools()
      ]

    # skill_manage：用户对话创建/查看/修改技能（async，走 raw_tools）。始终注入
    # （不依赖任何平台开关），按用户隔离，写盘后 bump skills_version 触发重建。
    skill_manage_tool = wrap_async_tool_with_error_handling(skill_manage)

    # 平台 deferred 工具：任一 <PLATFORM>_ENABLED 开关开启时，把已注册平台的能力
    # 生成为真工具（带 schema 强校验），交给 deferred filter 隐藏 schema，LLM 经
    # tool_search（语义检索）按需加载。
    # 注意：闸门是「是否有平台被启用」，不能只看 DCIM_ENABLED —— 否则只开
    # MGMT_ENABLED/SECURITY_ENABLED 时平台完全不会注册（历史 bug，P0-1）。
    middleware = []
    # middleware 链（before_model 正向执行）：
    # 1. DurableContextMiddleware — 注入 summary + 工具结果（防压缩丢失）
    # 2. SummarizationMiddleware — token 超阈值自动压缩旧消息
    # 3. DeferredToolFilterMiddleware — 海量工具 schema 隐藏/按需 promote
    from core.agent.middlewares.durable_context import DurableContextMiddleware
    from core.agent.middlewares.summarization import SummarizationMiddleware
    middleware.append(DurableContextMiddleware())
    middleware.append(SummarizationMiddleware())
    extra_prompt = ''
    from service.query.bootstrap import register_enabled_platforms

    platform_names = register_enabled_platforms()
    if platform_names:
      from service.query.base.tools_factory import build_all_tools
      from core.agent.deferred import build_deferred_setup, deferred_tools_prompt
      from service.query.base.manifest import build_category_manifest

      platform_tools = build_all_tools()
      deferred_tools, deferred_mw, _deferred_names, catalog_hash = build_deferred_setup(
        platform_tools, top_k=settings.DCIM_RETRIEVAL_TOP_K,
      )
      # deferred 工具 + tool_search 是 async / 返回 Command，走 raw 通道不包装
      raw_tools = deferred_tools + analysis_tools + [skill_manage_tool]
      middleware.append(deferred_mw)
      # P-2：<available-tools> 从全量工具名改为分类清单（build_category_manifest），
      # 每轮省 ~2.7k token；deferred_names 不再进 prompt。
      extra_prompt = deferred_tools_prompt(build_category_manifest())
      self._catalog_hash = catalog_hash
    else:
      raw_tools = list(analysis_tools) + [skill_manage_tool]
      self._catalog_hash = None

    # 权限不触发重建：RBAC 走 stream() 里 set_current_permissions 设置的运行时
    # contextvar 检查，见 build.py 说明。
    from core.agent.checkpointer import create_checkpointer
    self._checkpointer, self._checkpointer_closer = await create_checkpointer(settings)
    self._agent = build_rz_agent(
      model, tools,
      middleware=middleware, extra_prompt=extra_prompt, raw_tools=raw_tools,
      checkpointer=self._checkpointer,
    )

  async def stream(
    self,
    message: str,
    thread_id: str,
    user_id: str,
    session_id: str,
    permissions: set[str] | None = None,
    interaction_mode: str = 'trust',
    plan_confirmed: bool = False,
    plan: dict | None = None,
    interview_answers: dict | None = None,
    interview_action: str | None = None,
  ) -> AsyncIterator[str]:
    """Agent 流式对话。

    Args:
      message: 用户消息
      thread_id: LangGraph thread_id（用于 checkpoint）
      user_id: 用户 ID（透传给审计日志）
      session_id: 前端会话 ID
      permissions: 用户权限集合，用于 RBAC 工具过滤
      interaction_mode: 'trust' 自主执行 / 'plan' 先列计划确认后执行
      plan_confirmed: 计划模式下是否已确认执行
      plan: 已确认的执行计划（前端编辑后回传）
      interview_answers: 计划模式下访谈轮用户回答（{问题 id: 值}）
      interview_action: 访谈轮固定动作（answer/skip/chat），chat 退出计划走对话

    Yields:
      SSE 格式的字符串
    """
    perms = permissions or set()

    # 设置当前请求的 RBAC 上下文（工具调用时按此运行时检查）
    set_current_permissions(perms)
    # 设置当前请求的用户身份（技能工具/tool_search/caller 按此隔离用户自建技能）
    from core.agent.user_context import set_current_user_id
    set_current_user_id(user_id)

    # 确保 Agent 初始化
    await self._ensure_agent()

    from service.agent.streaming import format_sse, render_plan_instruction, save_messages

    # 计划模式：访谈/列计划（不执行）→ 确认后再执行。
    # interview_action='chat' 时退出计划模式走对话（聊点别的）。
    if interaction_mode == 'plan' and interview_action != 'chat':
      if plan_confirmed:
        # 执行轮：把回传的计划渲染成执行指令，替换本轮 user 消息喂给 LLM
        plan_instruction = render_plan_instruction(plan)
      elif interview_answers is not None or interview_action == 'skip':
        # 计划轮（访谈后 / 跳过访谈）：带已确认约束生成结构化计划 → SSE plan → 落库 → 结束
        from core.agent.planning import generate_plan, plan_to_markdown
        from service.query.base.manifest import build_category_manifest

        plan_obj = await generate_plan(message, build_category_manifest(), interview_answers)
        yield format_sse('plan', plan_obj.model_dump())
        await save_messages(session_id, user_id, message, plan_to_markdown(plan_obj))
        yield format_sse('done', {
          'session_id': session_id,
          'thread_id': thread_id,
          'phase': 'plan',
        })
        return
      else:
        # 首轮：先需求澄清。有不确定项 → SSE interview 事件等用户回答；无 → 直接给计划
        from core.agent.planning import generate_interview, generate_plan, plan_to_markdown
        from service.query.base.manifest import build_category_manifest

        manifest = build_category_manifest()
        interview = await generate_interview(message, manifest)
        if interview.needs_clarification and interview.questions:
          yield format_sse('interview', interview.model_dump())
          await save_messages(
            session_id, user_id, message,
            f'（正在确认需求：{len(interview.questions)} 个问题待回答）',
          )
          yield format_sse('done', {
            'session_id': session_id,
            'thread_id': thread_id,
            'phase': 'interview',
          })
          return
        plan_obj = await generate_plan(message, manifest)
        yield format_sse('plan', plan_obj.model_dump())
        await save_messages(session_id, user_id, message, plan_to_markdown(plan_obj))
        yield format_sse('done', {
          'session_id': session_id,
          'thread_id': thread_id,
          'phase': 'plan',
        })
        return
    else:
      plan_instruction = None

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
      plan_instruction=plan_instruction,
    ):
      yield sse_chunk

  async def teardown(self):
    """关闭时清理连接池。

    AsyncPostgresSaver 的连接池需在进程退出前关闭。
    """
    if self._checkpointer_closer is not None:
      await self._checkpointer_closer()
      logger.info('AgentService: Checkpointer 连接池已关闭')
