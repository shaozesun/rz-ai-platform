"""Agent 服务 — 单例管理 Agent 图实例 + Checkpointer 生命周期"""

import asyncio
import logging
from typing import AsyncIterator

from core.agent.build import build_rz_agent
from core.agent.middlewares.guardrail import set_current_permissions
from service.agent.streaming import stream_agent

logger = logging.getLogger(__name__)


async def _collect_recent_context(session_id: str, max_turns: int = 3) -> dict:
  """取会话最近几轮上下文（用户问题 + 用过的工具名），供意图路由判断省略延续追问。

  从 MongoDB 会话消息取最近若干条，提取最后 max_turns 个 user 消息文本（天然含对象实体）
  与 assistant 里的 `<tool-call name=...>` 工具名（中性事实，不断言意图）。无数据返回空 dict。
  """
  import re
  from service.rag.conversation.message_service import message_service
  try:
    msgs = await message_service.get_messages(session_id, limit=100)
  except Exception:
    return {}
  user_msgs: list[str] = []
  tools: list[str] = []
  for m in reversed(msgs):
    if m.role == 'user' and m.content and len(user_msgs) < max_turns:
      user_msgs.append(m.content.strip())
    elif m.role == 'assistant':
      for name in re.findall(r'<tool-call name="([^"]+)"', m.content or ''):
        readable = name.replace('__', '.')
        if readable not in tools and len(tools) < max_turns * 2:
          tools.append(readable)
    if len(user_msgs) >= max_turns and len(tools) >= max_turns:
      break
  if not user_msgs and not tools:
    return {}
  return {
    'user_messages': list(reversed(user_msgs)),
    'tools': list(reversed(tools))[:max_turns],
  }


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
    self._mode_gated_names: tuple[str, ...] = ()
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
      settings.WEB_ENABLED,
      settings.WEB_SEARCH_BASE_URL,
      settings.BROWSER_ENABLED,
      settings.BROWSER_EXPLORE_ENABLED,
      settings.BROWSER_MAX_SESSIONS,
      settings.BROWSER_SERVICE_URL,
      settings.AGENT_CHECKPOINT_BACKEND,
      settings.LANGFUSE_ENABLED,
      settings.LARK_ENABLED,
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

    # stage_skill_script：把技能自带的 scripts/ 复制进会话工作区，好让 analysis_exec
    # 能执行它（沙箱禁 subprocess，命令行方式跑不了）。技能系统只暴露 SKILL.md + references，
    # 没有它，带脚本的技能的脚本就是死文件。同样始终注入。
    from tools.skill_tools import stage_skill_script
    stage_skill_script_tool = wrap_async_tool_with_error_handling(stage_skill_script)

    # write_todos（计划模式的任务清单）：**常驻注册**。图的工具集是按配置指纹缓存的
    # （见 _ensure_agent），不能按请求加减工具，所以这里常驻、由计划模式每轮注入
    # PLAN_MODE_INSTRUCTION 决定用不用（对齐 MODE_INSTRUCTIONS 的做法）。
    # 非计划模式下模型偶尔调用也无害——只是更新一份前端不显示的清单。
    from tools.todo_tools import write_todos

    # 定时任务工具（对话用语言创建/管理定时报告）：仅 SCHEDULED_TASKS_ENABLED 时注入
    scheduled_tools = []
    if settings.SCHEDULED_TASKS_ENABLED:
      from tools.schedule_task_tool import schedule_task
      scheduled_tools = [wrap_async_tool_with_error_handling(schedule_task)]

    # 事件统计表（async，走 raw_tools）：飞书园区群通报 → 告警/工单关联，确定性出 xlsx。
    # 另含 `incident_progress`（单个事件的完整进展时间线）。
    # **不依赖 INCIDENT_STATS_ENABLED** —— 那个开关只管定时预设与后台轮询，
    # 对话里手动出一张表随时可用。
    from tools.incident_tools import incident_progress, incident_report
    incident_tools = [
      wrap_async_tool_with_error_handling(incident_report),
      wrap_async_tool_with_error_handling(incident_progress),
    ]

    # 事件单 ↔ DCIM 告警判定表（async，走 raw_tools）：先查判定表，缺的才补判。
    # 仅 DCIM_ENABLED 时注入——判定本身依赖 DCIM 告警数据；MGMT 关闭时仍可查表
    # （补算会返回明确错误，不静默）。
    event_alarm_tools = []
    if settings.DCIM_ENABLED:
      from tools.event_alarm_tools import event_alarm_link
      event_alarm_tools = [wrap_async_tool_with_error_handling(event_alarm_link)]

    # 文档更新检查（async，走 raw_tools）：25 条月度文档按内容哈希与上期比对。
    # 仅 DOC_UPDATE_CHECK_ENABLED 时注入（默认关，与定时预设同一开关）。
    doc_update_tools = []
    if settings.DOC_UPDATE_CHECK_ENABLED:
      from tools.doc_update_tools import doc_update_check
      doc_update_tools = [wrap_async_tool_with_error_handling(doc_update_check)]

    # 历史归档回读（async，走 raw_tools）：压缩会把旧消息整条删掉只留摘要，摘要是有损的
    # （实体名/编号可能被改写）。这两个工具让模型按需回读被压缩掉消息的原文。
    from tools.history_tools import build_history_tools
    history_tools = [
      wrap_async_tool_with_error_handling(t) for t in build_history_tools()
    ]

    # 上传文件工具（async，走 raw_tools）：列出/读取本会话已上传文件、把上传表格物化为
    # 沙箱数据集。始终注入（不依赖任何平台开关），文件上传是基础能力，不隐藏在 tool_search 后面。
    from tools.upload_tools import build_upload_tools
    upload_tools = [
      wrap_async_tool_with_error_handling(t) for t in build_upload_tools()
    ]

    # 讲解视频工具（async，走 raw_tools）：用会话里上传的 PPT 生成"每页解说+配音"的视频，
    # 并支持改一页/换语言后只重跑该页。仅 VIDEO_AGENT_ENABLED 时注入——**与表单页的视频
    # 功能（service/video，走 8010/8020/8030 插件）完全独立**，开关关闭时对话侧无任何变化。
    agent_video_tools = []
    if settings.VIDEO_AGENT_ENABLED:
      from tools.agent_video_tools import build_agent_video_tools
      agent_video_tools = build_agent_video_tools()

    # 飞书消息工具（async，走 raw_tools）：读取群消息 / 私聊记录，供自然语言触发。
    # 仅 LARK_ENABLED 时注入。
    feishu_tools = []
    if settings.LARK_ENABLED:
      from tools.feishu_tools import (
        fetch_group_messages, fetch_p2p_messages, list_joined_chats, list_group_members,
      )
      feishu_tools = [
        wrap_async_tool_with_error_handling(fetch_group_messages),
        wrap_async_tool_with_error_handling(fetch_p2p_messages),
        wrap_async_tool_with_error_handling(list_joined_chats),
        wrap_async_tool_with_error_handling(list_group_members),
      ]

    # 平台 deferred 工具：任一 <PLATFORM>_ENABLED 开关开启时，把已注册平台的能力
    # 生成为真工具（带 schema 强校验），交给 deferred filter 隐藏 schema，LLM 经
    # tool_search（语义检索）按需加载。
    # 注意：闸门是「是否有平台被启用」，不能只看 DCIM_ENABLED —— 否则只开
    # MGMT_ENABLED/SECURITY_ENABLED 时平台完全不会注册（历史 bug，P0-1）。
    middleware = []
    # middleware 链（wrap_model_call 外层先执行；before_model 正向执行）：
    # 1. DanglingToolCallMiddleware — 修补悬空 tool 调用/孤儿 tool 结果（序列化前置）
    # 2. DurableContextMiddleware — 注入 summary（防压缩丢失）
    # 3. SummarizationMiddleware — token 超阈值自动压缩旧消息
    # 4. DeferredToolFilterMiddleware — 海量工具 schema 隐藏/按需 promote
    from core.agent.middlewares.dangling_tool_call import DanglingToolCallMiddleware
    from core.agent.middlewares.durable_context import DurableContextMiddleware
    from core.agent.middlewares.summarization import SummarizationMiddleware
    middleware.append(DanglingToolCallMiddleware())
    middleware.append(DurableContextMiddleware())
    middleware.append(SummarizationMiddleware())
    extra_prompt = ''
    from service.query.bootstrap import register_enabled_platforms

    platform_names = register_enabled_platforms()
    if platform_names:
      from service.query.base.platform import is_mode_gated
      from service.query.base.tools_factory import build_all_tools, tool_name_to_capability
      from core.agent.deferred import build_deferred_setup, deferred_tools_prompt
      from service.query.base.manifest import (
        build_category_manifest, public_skill_tool_names, skill_usage_hint,
      )

      platform_tools = build_all_tools()
      # 父技能工具（skill__chart 等，params 为空、schema 极小）**不进 deferred**：技能正文靠
      # "调它才返回"，若连入口也藏起来，模型得先 tool_search promote 才够得着——实测它极少
      # tool_search，技能等于不存在。子能力（skill__chart__line）与用户自建技能仍 hidden。
      stay_visible = public_skill_tool_names()
      # 交互浏览工具成套常驻：navigate 被 promote 而 click 没被 promote，流程第二步就断。
      # schema 都很小（6 个），且仅在开关开启时才注册。
      if settings.BROWSER_EXPLORE_ENABLED:
        from service.query.browser.interactive import tool_names as browser_tool_names
        stay_visible = stay_visible | browser_tool_names()
      visible_tools = [t for t in platform_tools if t.name in stay_visible]
      # 模式门控平台的工具（web__search/web__fetch）：进 extra_hidden 而**不进 catalog** ——
      # tool_search 搜不到（模型无法自己 promote），schema 默认隐藏，硬调被 filter 拦；
      # 只有联网调研模式由编排层预 promote 才可见（见 core/agent/orchestrator.py）
      gated_names = tuple(
        t.name for t in platform_tools
        if is_mode_gated(tool_name_to_capability(t.name))
      )
      self._mode_gated_names = gated_names
      # 注意：门控工具必须**同时**出现在 agent 的工具列表里（否则 filter 无从"放出"——
      # 被 promote 了也没有对应工具可露，实测表现为「日志说放开了、模型却看不到工具」）。
      # 它们不进 catalog（tool_search 搜不到），只进 filter 的隐藏集合，等预 promote 放开。
      gated_tools = [t for t in platform_tools if t.name in gated_names]
      deferred_tools, deferred_mw, _deferred_names, catalog_hash = build_deferred_setup(
        [t for t in platform_tools if t.name not in stay_visible and t.name not in gated_names],
        top_k=settings.DCIM_RETRIEVAL_TOP_K,
        extra_hidden=gated_names,
      )
      # deferred 工具 + tool_search 是 async / 返回 Command，走 raw 通道不包装
      raw_tools = (deferred_tools + visible_tools + gated_tools + analysis_tools
                   + [skill_manage_tool, stage_skill_script_tool, write_todos] + upload_tools + scheduled_tools
                   + feishu_tools + history_tools + agent_video_tools + incident_tools
                   + event_alarm_tools + doc_update_tools)
      middleware.append(deferred_mw)
      # P-2：<available-tools> 从全量工具名改为分类清单（build_category_manifest），
      # 每轮省 ~2.7k token；deferred_names 不再进 prompt。技能另有 <skills> 提示段：
      # 技能正文不进上下文，但必须让模型知道"有技能、该去调"（对齐 DeerFlow 的 skill_index）。
      extra_prompt = deferred_tools_prompt(build_category_manifest()) + skill_usage_hint()
      self._catalog_hash = catalog_hash
    else:
      raw_tools = (list(analysis_tools) + [skill_manage_tool, stage_skill_script_tool, write_todos] + upload_tools
                   + scheduled_tools + feishu_tools + history_tools + agent_video_tools
                   + incident_tools + event_alarm_tools + doc_update_tools)
      self._catalog_hash = None
      self._mode_gated_names = ()

    # 权限不触发重建：RBAC 走 stream() 里 set_current_permissions 设置的运行时
    # contextvar 检查，见 build.py 说明。
    from core.agent.checkpointer import create_checkpointer
    self._checkpointer, self._checkpointer_closer = await create_checkpointer(settings)
    self._agent = build_rz_agent(
      model, tools,
      middleware=middleware, extra_prompt=extra_prompt, raw_tools=raw_tools,
      checkpointer=self._checkpointer,
    )

    # 可观测性追踪回调（Langfuse）不在这里缓存：handler 携带跨 run 的上下文状态，全进程
    # 共享会让并发请求互相串成父子节点。每请求在 stream() 里构造一次（构造本身不建 span）。

  async def stream(
    self,
    message: str,
    thread_id: str,
    user_id: str,
    session_id: str,
    permissions: set[str] | None = None,
    interaction_mode: str = 'trust',
    task_mode: str = 'normal',
    web_search: bool = False,
    uploaded_filenames: list[str] | None = None,
  ) -> AsyncIterator[str]:
    """Agent 流式对话。

    Args:
      message: 用户消息
      thread_id: LangGraph thread_id（用于 checkpoint）
      user_id: 用户 ID（透传给审计日志）
      session_id: 前端会话 ID
      permissions: 用户权限集合，用于 RBAC 工具过滤
      interaction_mode: 'trust' 自主执行 / 'plan' 计划模式——执行过程中用 write_todos
        维护任务清单、前端实时显示进度（**没有确认门**，对齐 DeerFlow）
      task_mode: 'normal' 常规 / 'full_analysis' 全量分析（优先多维度数据分析，每轮注入模式指令）
        / 'research' 联网调研（放开联网工具 + 注入调研方法论）
      web_search: 前端「联网搜索」开关。与 task_mode='research' 等价地放开联网工具，
        但**不注入**任何调研规范（能力与规范刻意解绑）
      uploaded_filenames: 本轮消息关联的已上传文件名，触发 <current_uploads> 自动注入

    Yields:
      SSE 格式的字符串
    """
    perms = permissions or set()

    # 设置当前请求的 RBAC 上下文（工具调用时按此运行时检查）
    set_current_permissions(perms)
    # 设置当前请求的用户身份（技能工具/tool_search/caller 按此隔离用户自建技能）
    from core.agent.user_context import set_current_thread_id, set_current_user_id
    set_current_user_id(user_id)
    # 会话身份：**工具执行期**就要有值。原来只有落库阶段（streaming._normalize_file_urls）
    # 才设，导致按会话取数据的工具（讲解视频的 video_* 等）拿到空 thread → 报"无法识别
    # 当前会话"（模型转述成"会话状态问题"），且会话归属校验被跳过（能按 task_id 读到别人会话）。
    set_current_thread_id(thread_id)

    # 确保 Agent 初始化
    await self._ensure_agent()

    # 多标签编排 Step1+2：意图识别 → 查绑定表 → 预 promote 工具组。
    # 无预 promote（detect/空标签/无平台启用）时为空 dict，agent 正常走 tool_search。
    # P1-1：intent_labels 词汇表未定义时 build_intent_bindings() 恒为空，
    # route_to_tools 必然返回 None，此时 plan_query 的结构化 LLM 调用产出会被
    # 100% 丢弃——短路跳过，省一次白付的 LLM 调用。能力集稳定后在 Capability 上
    # 填 intent_labels，词汇表非空时会自动恢复走 Step1+2 快路径。
    initial_state = {}
    from service.query.base.intent_bindings import all_intent_labels
    # 联网调研模式/联网开关即便意图标签为空也要走编排：唯一的目的是把模式门控的联网工具放出来
    if self._catalog_hash and (all_intent_labels() or self._mode_gated_names):
      from core.agent.orchestrator import build_initial_state
      recent = await _collect_recent_context(session_id)
      initial_state = await build_initial_state(
        message, self._catalog_hash, history=recent, task_mode=task_mode,
        web_search=web_search, gated_names=self._mode_gated_names,
      )

    from core.agent.prompts import MODE_INSTRUCTIONS, PLAN_MODE_INSTRUCTION
    mode_instruction = MODE_INSTRUCTIONS.get(task_mode)
    # （
    # 视频生成原先还有个「输入栏开关」：开/关只影响是否注入流程引导，而工具一直注册着，
    #   开不开都能做视频——已按用户要求删掉那个开关；视频流程引导改由常驻的元规则 12
    #   + skills/agent_video/SKILL.md + 工具返回里的下一步说明承担。）
    # 计划模式：同样正交叠加——只注入「怎么用 write_todos 维护任务清单」的纪律，
    # 工具本身常驻注册（见 _rebuild 的 write_todos 说明）。
    if interaction_mode == 'plan':
      mode_instruction = (
        f'{mode_instruction}\n\n{PLAN_MODE_INSTRUCTION}' if mode_instruction
        else PLAN_MODE_INSTRUCTION
      )

    # 追踪 handler 每请求新建（见 _rebuild 末尾说明）
    from core.agent.tracing import build_tracing_callbacks

    async for sse_chunk in stream_agent(
      agent=self._agent,
      message=message,
      thread_id=thread_id,
      user_id=user_id,
      session_id=session_id,
      initial_state=initial_state,
      mode_instruction=mode_instruction,
      uploaded_filenames=uploaded_filenames,
      tracing_callbacks=build_tracing_callbacks(),
    ):
      yield sse_chunk


  async def teardown(self):
    """
    关闭时清理连接池。

    AsyncPostgresSaver 的连接池

    需在进程退出前关闭。
    """
    if self._checkpointer_closer is not None:
      await self._checkpointer_closer()
      logger.info('AgentService: Checkpointer 连接池已关闭')
