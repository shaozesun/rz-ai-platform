# AGENTS.md

This file provides guidance to Codex (Codex.ai/code) when working with code in this repository.

## 常用命令

```bash
# 安装后端依赖（遇 SSL 证书问题加 --trusted-host pypi.org --trusted-host files.pythonhosted.org）
pip install -r requirements.txt

# 红外巡检图片质量检测自检（24 项，纯 assert、不连库）：标签→判定（含 fail-closed、
# 旧中文标签判未识别）、只对有图片设备出行、分批不丢数、按表头解析新结果表、文件名映射
# （重名消歧 + 位置回退）、SSE 解析、结果表定位、列与行键一致、缓存命中的设备补图 /
# 本地已有则复用、缓存写入必须是 pymongo UpdateOne
python -m service.infrared_quality.tests.test_infrared_quality

# 页面截图自检（纯 assert，不连网、不启浏览器）：
# recipes = 配方参数/主机白名单/截图原子发布；interactive = 交互 6 工具/会话 id/导航校验/返回整形
python -m service.query.browser.tests.test_recipes
python -m service.query.browser.tests.test_interactive
# 旁路服务自测（fixture 页，验证截图/表格抽取/就绪超时 + navigate→click→screenshot→close；
# 需在 browser 容器内跑）
docker compose -f deploy/docker-compose.yaml exec browser python test_app.py

# 启动后端
python main.py

# 后端开发模式 (热重载)
uvicorn main:app --reload --host 0.0.0.0 --port 8000

# 前端开发
cd web && npm run dev        # Vite 开发服务器 (localhost:5173)
cd web && npm run build      # 生产构建 (tsc -b && vite build) → dist/
cd web && npm run lint       # oxlint 静态检查
cd web && npm run preview    # 本地预览生产构建

# Docker Compose 部署（含 nginx + MongoDB + Redis + Milvus + Loki + Promtail + Grafana）
cd deploy && docker compose up -d
```

## 架构

润泽 AI 平台 — 数据中心统一 ToB AI 服务（对话式运维助手），由 risk-api（安全隐患检测）和 rz-rag（RAG 知识库）合并而成，并演进出了 Agent 智能体平台（意图路由 → 平台工具调用 → 数据分析沙箱 → 前端本地渲染图表）。认证体系为手机号+密码+RBAC 权限模型。

### 分层

```
api/v1/     →  service/    →  core/       →  config/
  (路由)        (业务逻辑)      (基础设施)      (配置/连接)
                  ↓
               models/ (Pydantic DTO)
```

- **api/v1/** — FastAPI Router：`auth.py`, `admin.py`, `rag.py`, `chat.py`, `risk.py`, `video.py`, `stats.py`, `feedback.py`, `health.py`, `agent.py`
- **service/** — 业务逻辑层：`rag/`, `risk/`, `auth/`, `video/`, `agent/`, `query/`, `coze_proxy/`
- **core/** — 基础设施：`model_gateway.py` (LLM 统一入口), `auth_engine.py` (JWT), `rbac.py` (权限), `sms.py`；`agent/` (LangGraph Agent 图), `sandbox/` (数据分析沙箱), `scheduler/` (LLM 调度器)
- **config/** — 配置与连接：`settings.py` (pydantic-settings, 从 `.env` 加载), `mongodb_conn.py`, `redis_conn.py`, `logger_config.py`, `trace_id.py`
- **models/** — Pydantic 数据模型：`user.py` (User/Role/Permission + 预置角色), `risk/schemas.py`, `rag/schemas.py`, `video.py`, `agent_schemas.py`
- **middleware/** — ASGI 中间件：`auth_middleware.py` (JWT 注入 user_id/permissions 到 `request.state`)
- **repository/** — RAG 数据层：`vector_store.py` (Milvus), `loader.py`, `splitter.py`, `sparse_embedder.py`
- **tools/** — Agent 工具：`rag_tools.py` (知识检索), `mock_tools.py` (当前时间), `mock_ops_tools.py` (发通知), `analysis_tools.py` + `analysis_file_tools.py` (数据分析沙箱)
- **web/** — React + TypeScript + Vite + Tailwind CSS 4 + shadcn/ui 前端
- **skills/** — Agent 技能目录（`chart`、`html_dashboard`、`skill_creator` 等公开技能 + `users/<uid>/` 用户自建技能，SKILL.md 自动发现进工具索引）
- **prompts/** — LLM 提示词模板：`rag/`, `risk/`
- **data/** — BM25 稀疏向量词表 (`sparse_vocab_group_default.pkl`)
- **embedded/** — 视频生成产物输出目录
- **docs/** — 架构设计与实现记录（Agent 架构、沙箱、能力目录、接口文档等）
- **deploy/** — Docker Compose 生产部署配置 + 备份恢复脚本

### 认证鉴权

- 手机号 + 密码登录，登录/注册/重置密码需图形验证码（防暴力破解）
- 开发模式 `SMS_DEV_MODE=true` 跳过图形验证码校验
- 开发模式 `PERMISSION_OPEN_MODE=true` 时认证用户自动获得全部权限（跳过 RBAC 检查）
- JWT access_token (30min) + refresh_token (7d)，RSA 密钥对签发
- RBAC: User → Role → Permission (`resource:action`)
- 新用户默认 `rag_basic` 角色 (仅 `rag:chat`)，其他功能需申请 → Root 审批
- `@require_permission("risk:check")` 装饰器保护端点，中间件检查登录态后从 MongoDB 加载权限

**图形验证码:** 登录/注册/重置密码需输入验证码，防暴力破解。两种方案通过配置切换：

| 方案 | 配置值 | 说明 |
|------|--------|------|
| Pillow 数学算式 (当前) | `CAPTCHA_PROVIDER=pillow` | 本地生成，零外部依赖，Redis 存答案 120s |
| Cloudflare Turnstile | `CAPTCHA_PROVIDER=turnstile` | 需域名 + `TURNSTILE_SITE_KEY` / `TURNSTILE_SECRET_KEY` |

切换方法：
- 前端 `web/src/pages/LoginPage.tsx` 第 11 行常量 `CAPTCHA_PROVIDER`
- 后端 `.env` 中 `CAPTCHA_PROVIDER` + Turnstile 密钥（如用 Turnstile）
- Pillow 模式无需额外配置，`SMS_DEV_MODE=true` 可跳过验证码

### 核心模块

- `core/model_gateway.py` — 统一 LLM 网关 (单例)，方法：`chat()`, `chat_stream()`, `vision()`, `embedding()`, `batch_score()`。视觉和对话客户端可指向不同 API 端点
- `core/auth_engine.py` — RSA JWT 签发/验证/刷新 + Redis 黑名单
- `core/rbac.py` — `load_role_permissions()` 从 MongoDB 加载 + 内存缓存，`@require_permission` 装饰器
- `middleware/auth_middleware.py` — ASGI 中间件，提取 Bearer token → 验证 → 注入 `request.state.user_id/permissions`
- `repository/vector_store.py` — Milvus 向量库管理，BM25 稀疏 + Dense 混合检索
- `core/scheduler/` — LLM 调用优先级调度器（见下）

### Agent 智能体平台（核心）

对话式运维助手，LangGraph 驱动，自主决定是否调用工具（知识检索 / 平台数据 / 沙箱分析）。

**总链路：**
```
前端 ChatPage → ChatArea (SSE)
  → POST /api/v1/chat/agent (api/v1/agent.py, 权限 ai:agent, thread_id=rz-agent-{session_id})
  → service/agent/agent_service.py (AgentService 单例)
  → core/agent/build.py build_rz_agent (LangGraph 图 + middleware + checkpointer)
  → service/agent/streaming.py (SSE 事件流 + LoopDetector + RBAC 工具级校验 + 落库/审计)
```

- `service/agent/agent_service.py` — AgentService 单例：`stream()` 设 RBAC contextvar (`guardrail.set_current_permissions`) → `_ensure_agent()` 按开关注册平台、装配工具、构建图、创建 Checkpointer → 多标签编排 `build_initial_state` → `stream_agent`
- `core/agent/prompts.py` — `SYSTEM_PROMPT`（工作原则、`<chart>` 图表输出规范、报告生成规范、HTML 看板生成规范）
- **工具集**：基础工具 `[rag_knowledge_search, get_current_time, send_notification]`；`AGENT_ANALYSIS_ENABLED` 时追加分析工具 `build_analysis_tools()`；任一 `<PLATFORM>_ENABLED` 开启时把已注册平台能力生成为平台工具（deferred，见下）；另有历史归档回读 `history_search` / `history_read`（见「摘要」）
- **middleware 链**（`core/agent/middlewares/`）：`DanglingToolCallMiddleware`（修补悬空 tool 调用/孤儿 tool 结果，防严格后端 400）→ `DurableContextMiddleware`（注入摘要+归档状态，防压缩丢失）→ `SummarizationMiddleware`（超阈值压缩旧消息）→ `DeferredToolFilterMiddleware`（海量工具 schema 隐藏/按需 promote）。另有 `audit.py` / `guardrail.py` / `loop_detect.py` / `rag_inject.py` / `tool_error.py`
- **Checkpointer** — `core/agent/checkpointer.py` `create_checkpointer()`：`AGENT_CHECKPOINT_BACKEND` 三选（memory 默认 / postgres），postgres 用 `AsyncPostgresSaver` + 连接池，进程退出前 `teardown()`
- **promoted channel 必须带 `merge_promoted` reducer** — `core/agent/state.py` 的 `RzAgentState.promoted` 必须是 `Annotated[..., merge_promoted]`：`tool_search` 每命中一次就写一次 promoted，若退化成 last-write-wins，会把编排层预 promote 的工具（联网调研模式的 `web__search`/`web__fetch`）整体顶掉 —— 实测 bug：模型一轮里只要调过一次 `tool_search`，联网工具就再也看不见了（同 hash 累积、换 hash 整体替换）
- **摘要** — `core/agent/summarization.py`：`AGENT_SUMMARIZE_ENABLED`、`AGENT_SUMMARIZE_TRIGGER`（**`tokens:32000`，绝对 token，对齐 DeerFlow 默认值**）、`AGENT_SUMMARIZE_KEEP`（**`tokens:6000`**，token 型：`messages:10` 在 32k 触发时常常 cutoff=0）。**别用窗口比例**（`fraction:0.6` × 262144 = 157k → 长会话永不触发压缩；2026-09-17 实测：上下文 63k 从未压，冷启动首 token 27s）。触发优先用**模型上报的真实 token**（最近一条 AIMessage 的 `usage_metadata.total_tokens`，依赖 `model_gateway.as_langchain_model(stream_usage=True)`），无则回退 `字符数//2` 估算。压缩时按精确对象 rescue：**本轮全部消息**（最后一条真实用户消息起，与受保护结果重叠的用户消息按身份去重）+ `AGENT_SUMMARIZE_PROTECTED_TOOLS`（默认公司/部门列表这两个字典类能力）的**完整 tool 轮次**（全部 tool_calls 及配对 ToolMessage，防并行调用悬空）→ 压缩只发生在轮与轮之间
- **首 token 与上下文长度的关系** — 模型输出前要 prefill 整段输入（期间零输出），本端点实测 ≈2300 tokens/s：16k token ≈4s、63.6k ≈27s（冷启动），同前缀第二次仅 0.7s（prefix caching）。所以长会话「隔几小时回来第一次问」最慢，连续追问很快；压小常驻上下文是唯一解。兜底：`AGENT_FIRST_TOKEN_TIMEOUT=30`（首事件，不含 `on_chat_model_start`）与 `AGENT_EVENT_TIMEOUT=360`（中途静默）分工不同，前者超时 `stop=first_token_timeout`；`AGENT_MAX_TOOL_OUTPUT_CHARS=12000`（对齐 DeerFlow）
- **历史归档回读** — `core/agent/history_archive.py`：压缩成功后被删消息的**原文**落盘 `{AGENT_ANALYSIS_DATA_DIR}/{thread_id}/task_history/history.jsonl`（只存可见文本 + tool_calls 参数；`source_id = r+sha256(全文)[:32]` 幂等；上限 32 批 × 256 条 × 16000 字）。模型用 `history_search`（中文 bigram 检索，返回 source_id + 600 字摘录）/ `history_read`（4000 字分页读原文）回读，工具按当前请求 thread_id 隔离。`state.task_history` 存批次索引，`DurableContextMiddleware` 渲染成「历史归档」段提示模型去回读而不是凭印象补全

**意图路由与工具选择（promote 机制）：**
```
用户消息 → core/agent/query_planner.py (LLM 意图 → IntentPlan: single/parallel/detect/chain)
        → core/agent/orchestrator.py build_initial_state/route_to_tools (按绑定表 promote 工具组)
        → core/agent/deferred.py DeferredToolFilterMiddleware (隐藏未 promote 工具 schema)
        → 未命中时 LLM 用 tool_search 语义检索 promote 所需工具
```
- 能力集稳定前 `intent_bindings.all_intent_labels()` 为空时跳过编排（省一次白付的 LLM 调用）
- 并行/串行工具调用由 LangGraph ToolNode 执行，调度方式（单/多/串行/并行）由 LLM 自主决定

**平台能力注册（见「平台能力」节）：**
```
service/query/bootstrap.py register_enabled_platforms()
  → MGMT_ENABLED/SECURITY_ENABLED/WEB_ENABLED 开关注册 + skills 平台无开关常驻
  → base/manifest.py build_category_manifest() 生成 <available-tools> 分类清单
  → base/tools_factory.py build_all_tools() 生成为 StructuredTool（schema 强校验 + 输出封顶
    AGENT_MAX_TOOL_OUTPUT_CHARS：先按记录级 pop（顶层裸数组也算记录列表），仍超预算则
    「归档全文 + 结构化摘要 build_synopsis() + history_read 回读指引」，归档不可用才回退字符级截断）
```

**分析出图 / 报告（见「数据分析沙箱」节）：** `analysis_load` 物化全量数据 → `analysis_exec` 沙箱跑 pandas 聚合 → 返回 `chart_data` 组装 `<chart>` 标签（前端 ChartRenderer.tsx 本地渲染，不出内网）；产物文件回传 `artifacts`，组装 `<file url name>` 标签供下载。用户要「看板/大屏」时走 **HTML 看板**路径：`analysis_write_file` 写自包含 `.html`（数据内联 + echarts）→ `analysis_exec(file=...)` → `<file>` 标签 → 前端 `ArtifactPanel.tsx`/`HtmlPreview.tsx` sandbox iframe 本地预览（echarts 打包内联，不出内网）。

### 平台能力（综合管理 / 安防 / 联网 / 页面截图 / 技能）

统一检索层 `service/query/base/`（`platform.py` Platform 注册、`capability.py` Capability、`tools_factory.py` 工具生成、`retriever.py` 检索、`manifest.py` 分类清单、`intent_bindings.py` 绑定表）。每个平台一套 `capabilities.py`（能力声明）+ `endpoints.py`（EndpointSpec 接口声明）+ `client.py`（通用调用器）三件套，params 用 snake_case、经 `param_map` 映射为接口 camelCase。

> **DCIM 平台已删除**：原 `service/query/dcim/`（6 条告警/资产/容量/用电能力）是 Phase 1 的**门面 mock**，无真实 HTTP 实现，已整体移除。`DCIM_ENABLED` / `DCIM_BASE_URL` / `DCIM_API_TOKEN` / `DCIM_TIMEOUT` / `DCIM_RETRIEVAL_TOP_K` 配置项**保留**，留给将来接入真实 DCIM 接口。DCIM 侧数据目前走 Kafka 接入（`service/dcim_ingest/`）。

| 平台 | 能力规模 | 开关 | 说明 |
|------|---------|------|------|
| mgmt 综合管理平台 | 38 条 | `MGMT_ENABLED` | 人员(org)/巡检(inspection)/维护(maintenance)/设备(device)/文档(document)/工单(work_order)/配置(config) 分类，如按姓名查人、设备台账、设备维护计划、设备类型树、巡检项模板、红外温度检测、巡检工单、待办工单、变更工单、故障/问题/事件工单详情、配置管理、任务列表、标准规范等 |
| security 安防平台 | 0 条 | `SECURITY_ENABLED` | 占位空元组，接口文档到位后登记 |
| web 联网 | 2 条 | `WEB_ENABLED`（默认关） | `web__search`（自建 SearXNG 检索）/ `web__fetch`（抓取公网网页）。**模式门控**：只在「联网调研」模式（`task_mode='research'`）或输入栏「联网搜索」开关（`web_search=true`）下可见，其他情况不进分类清单/不进 planner 词汇表/不进 tool_search 检索目录 |
| browser 页面截图 | 按已校准配方注册 + 交互模式 7 条 | `BROWSER_ENABLED` / `BROWSER_EXPLORE_ENABLED`（默认关） | 按声明式配方进入内网系统页面、设参数筛选后截图，并返回页面表格与正文；交互模式另提供 navigate/snapshot/click/get_text/screenshot/record/close 逐步操作（record 在当前页面录固定时长，交付关键帧 + 可选 webm；危险按钮在快照标 blocked、点击被拒）。执行在独立 Playwright 旁路容器（`deploy/browser/`），后端只持有配方，会话在容器内按 thread_id 派生 id 共享（多 worker 可命中同一会话）。截图走会话产物链路内联显示。**模板配方 `enabled=False`，未校准前不注册能力**；要精确数据请用对应平台的数据查询，不要用截图替代。详见 `docs/browser-capture.md` |
- **联网调研模式（research）/ 联网搜索开关** — 能力范围：仅 `web.search`/`web.fetch`（搜索走自建 SearXNG，`WEB_SEARCH_BASE_URL`；抓取只允许公网 http/https）。门控三层（缺一不可）：① 构建期 `MODE_GATED_PLATFORMS` 排除出 `<available-tools>` 与意图绑定表；② `build_deferred_setup(extra_hidden=...)` 让它不进 `tool_search` 检索目录、schema 默认隐藏、硬调被拦；③ `orchestrator.build_initial_state` 预 promote 才可见（`task_mode='research'` 或 `web_search=true` 任一即放开）。SSRF：`service/query/web/url_safety.py`（`not is_global or is_multicast`，含 CGNAT 100.64/10）+ **重定向逐跳复校验**（`follow_redirects=False`）。审计：每次检索/抓取 fire-and-forget 落 `web_queries`（query/url 原文 + 来源域名），出方向不做管控，审计是唯一追溯手段。事实污染：`MODE_INSTRUCTIONS['research']` 要求外部来源就地 `[citation:标题](URL)` + 文末 Sources 段，并强制「内外分开陈述、冲突以内部数据为准」；`citations` 已进不可信内容 denylist。**只放开工具、不注入规范**是开关与任务模式的分界（刻意解绑）
| skill 技能平台 | analysis 1+0（怎么算：聚合形状/pandas 写法/交互表格整理）、chart 1+26（怎么画：选型+字段规范）、html_dashboard 1+1、skill_creator 1+0、deep_research 1+0（联网调研方法论，配合联网调研模式）、company_profile 1+0（公司背景档案，按需加载，不常驻；元规则 11 引导）、consulting_analysis 1+2（专业分析报告方法论：Phase1 分析框架 + Phase2 成文，含数据真实性协议） （另有用户自建技能动态增减） | 无开关常驻 | `skills/` 目录自动发现（SKILL.md + references），零代码接入新技能；用户自建技能存 `skills/users/<uid>/`，按用户隔离 |

- 默认开关全 False，Agent 只保留知识检索/时间/通知基础工具
- **用户自建技能（复刻 DeerFlow skill-creator）**：`skill_manage` 工具（受 `ai:agent` 门控）让用户对话创建/查看/修改技能，按用户隔离存 `skills/users/<uid>/<name>/`（公开技能在 `skills/<name>/`）；技能名全局唯一，写盘过静态+LLM 语义安全扫描（`AGENT_SKILL_LLM_SCAN`，fail-closed）。`skill_creator` 技能引导访谈→起草→创建。热加载：写盘后 `bump_skills_version()` → 下一条消息触发 agent 重建。用户技能不进 `<available-tools>`/路由词汇表，按需 tool_search 命中。**部署注意：`skills/users/` 是运行期写入，Docker 需挂 volume，否则容器重建丢技能**
- **页面截图平台（browser）** — 与数据查询**刻意分开**：截图解决「页面长什么样」，精确数据仍走 mgmt/dcim_meta。执行器是独立 `browser` 容器（`deploy/browser/app.py`，Playwright + storageState），后端只发声明式配方（`service/query/browser/recipes.py`：路径 + `{参数}` + steps + ready + extract）；生产 `uvicorn --workers 4` 下不能用进程内有状态会话，故交互浏览（`BROWSER_EXPLORE_ENABLED`，7 个工具 navigate/snapshot/click/get_text/screenshot/record/close）的会话也放在该容器内，按 `thread_id` 派生 id 共享、不暴露给模型，超 `BROWSER_MAX_SESSIONS` 按最近使用淘汰、空闲 `BROWSER_SESSION_IDLE_SECONDS` 回收，超时不关会话。截图落 `{会话目录}/browser/`，**原子发布不覆盖**（O_EXCL + 硬链接 + inode 校验），返回 `<file>` 由 `FileCard` 内联（截图/关键帧走图片内联，webm 走 blob → `<video>`）。`record` 在当前页面录固定时长（5~120s，全局只允许一路），交付关键帧 + 可选 webm（CDP `Page.startScreencast` + ffmpeg 合成，旁路镜像需装 ffmpeg）。**点击黑名单**：快照给命中危险词（删除/提交/审批等）的元素标 `blocked`、`click` 直接拒绝，词表 `BROWSER_CLICK_DENYLIST` 整体覆盖内置激进表。登录走服务账号登录配方或 `deploy/scripts/browser_login.py` 手动 storageState；配方必须先用 `deploy/scripts/browser_probe.py` 探查真实页面后校准，未校准配方 `enabled=False` 不注册。开关/部署/失败 stage 见 `docs/browser-capture.md`
- mgmt 能力详情见 `service/query/mgmt/capabilities.py` 顶部 docstring；EndpointSpec 字段（method/path/param_map/defaults/default_params/result_path/page_meta/value_map 等）见 `service/query/mgmt/endpoints.py`
- 已实测接口的坑：URL 路径含 "get"（如 `/equipment/get/dept`）实际是 **POST**（GET 返 405）；path 占位符参数必须必填（defaults 只兜底 query/body 参数）；MyBatis-Plus 分页信封 `records/total/current/pages/size` 用 `page_style='page_size_alt'`
- **不可信内容防护** — `core/agent/untrusted_content.py`（照搬 DeerFlow `neutralize_untrusted_tags`）：平台返回在包「不可信数据」边界**之前**做两件事——转义框架/注入标签（`<durable_context_data>`/`<system>`/`<prompt>` 等 13 个，只转 `<>` 不转 `&`/引号）、把数据里伪造的边界标记（`【数据结束】`/`[历史对话摘要]`）惰性化为 `（…）`。普通 HTML/XML（`<div>`/`温度<10℃`）原样通过。边界常量唯一定义在该模块（`tools_factory` 导入）。`verify_denylist_coverage()` 扫 `core/agent`+`service/agent`+`service/query`+`tools` 里成对的框架块标签，报出未分类的（防「加了新框架块却忘了封锁」的漂移）。**多通道**：`@sanitizing` 装饰器已接飞书消息（`tools/feishu_tools.py`，外部可写）、上传文件（`tools/upload_tools.py`）、历史归档回读（`tools/history_tools.py`）——放在 `@tool` 之下（先净化再由 @tool 包装）

### 数据分析沙箱

LLM 需要做全量/统计/聚合/报告时，数据先物化到沙箱（原始记录不进上下文），再在受限子进程跑 pandas。

- `tools/analysis_tools.py` — `analysis_load`（按平台能力分页拉**全量**数据落盘，只回 dataset_id/rows/columns/预览，受 `AGENT_ANALYSIS_MAX_ROWS` 保护，**超限即拒绝**：返回 `error` + `total_rows`，不物化截断样本）+ `analysis_exec`（跑 pandas，`result` 变量 → `chart_data`；产物文件 → `artifacts`）；`tools/analysis_file_tools.py` 提供 `analysis_write_file`/`analysis_str_replace`/`analysis_exec(file=...)` 脚本化写报告
- **成文交付的工作区** — `analysis_write_file` 支持保留的 `dataset_id='workspace'`：纯联网调研等**没有 analysis_load 数据集**的场景，把文字成品（Markdown 报告/来源清单）写到 `{AGENT_ANALYSIS_DATA_DIR}/{thread_id}/workspace/`，**不要求 data.csv**，返回值给服务端生成的权威 url，模型原样输出 `<file url name/>` 交付；下载端点按文件名扫会话目录（`datasets.find_artifact_dir`），无需数据集即可用。`_evict_if_needed` 已跳过该目录（否则报告会被当"最旧数据集"删掉）。**目的：成品落文件、对话只给摘要**。**默认交付格式是 Markdown**（`PDF/PPT/Excel/Word` 只在用户明确点名时才生成；用户要「分析/方案/计划」这类**内容**时不产文件）
- `core/sandbox/datasets.py` — 数据集落盘 `{AGENT_ANALYSIS_DATA_DIR}/{thread_id}/{dataset_id}/data.csv + meta.json`，按会话隔离，超 `AGENT_ANALYSIS_MAX_DATASETS_PER_SESSION` 删最旧
- `core/sandbox/runner.py` — `run_python()` 隔离执行：`python -I` 子进程、RLIMIT_AS 内存 + RLIMIT_CPU、BLAS/OpenMP 强制单线程（`OPENBLAS_NUM_THREADS=1` 等，防多核 OpenBLAS 线程栈顶破 512MB 地址空间）、socket/urllib 置空禁网络（保留可被 ssl 子类化）、subprocess/os.system/os.fork/exec*/pty 置空禁子进程、外部 `asyncio.wait_for` 超时；产物扫描剔除 data.csv/meta.json/_chart_data.json/.py，文档产物剔除冗余图片
- 报告生成：脚本内 pandas + matplotlib + WeasyPrint(HTML→PDF) / python-pptx / pandas to_excel；中文字体 `RZ_CJK_FONT` 注入子进程
- 产物下载：`GET /api/v1/chat/agent/artifacts/{thread_id}/{dataset_id}/{filename}`（会话归属 + `ai:agent` 权限 + 路径穿越校验）
- 图表渲染：LLM 输出 `<chart type=...>JSON</chart>`，前端 `web/src/components/ChartRenderer.tsx` 用 @ant-design/plots + @ant-design/graphs 本地渲染 26 种图（plots 17 + graphs 5 + 表格 1 + 地图降级 3），数据不出内网
- 看板渲染：用户要「看板/大屏」时，LLM 走 `analysis_write_file` 产出自包含 HTML 看板（数据内联 + echarts），前端 `web/src/components/ArtifactPanel.tsx` + `HtmlPreview.tsx` 用 sandbox iframe + 打包内联 echarts（`echarts.min.js?raw`）本地渲染；`.html` 产物仍强制附件下载（api/v1/agent.py `_ACTIVE_CONTENT_TYPES`）

### LLM 调度器

`core/scheduler/` 统一管控 LLM 并发，防止后端被打爆。

- `TaskScheduler` 单例：协调 `DistributedSemaphore`（Redis 信号量）+ `PriorityTaskQueue`（Redis 优先级队列）
- `main.py` lifespan 里 `scheduler.initialize()`，`patch_rag_service` 把 RAG 的 LLM 调用注入调度
- 优先级 = `TaskType`（业务场景基础优先级）× `RoleLevel`（角色权重），`PriorityCalculator` 计算
- 非流式任务无槽位入队等待（Redis keys `scheduler:slots/queue/notify`）；流式请求（SSE 对话）只抢槽不排队，超时抛 `SlotNotAvailable` → API 返回 503；Redis 不可用/调度器禁用时降级 `LocalSemaphore`

### Risk 模块 (从 risk-api 迁移)

**两阶段检测流水线：**
```
用户上传图片 → Phase 1 (VLM 视觉模型)
    ├── 非机柜场景 → 直接返回通用安全检测结果
    └── 识别到 4 种消防控制柜 → Phase 2
        ├── CV 检测 (优先): ORB(2000特征点) → FLANN LSH → RANSAC 单应矩阵 → LAB L通道亮灭(阈值145)
        └── CV 失败 → 降级 VLM 视觉对比 (用户图 + 参考图)
```

- `service/risk/cv_detection.py` — CV 管线（OpenCV），启动时从 `template/` 加载 4 种机柜配置
- `service/risk/detection_service.py` — 两阶段调度 + 图片压缩(Pillow, >300KB 自动缩至 ≤2048px)
- `service/risk/fire_safety_service.py` — 消防配置推荐，完整模式 (13 个建筑参数) + 简化模式 (兼容旧接口)
- `service/risk/report_service.py` — Markdown + Word (python-docx) 双格式报告生成
- `service/risk/history_service.py` — 历史记录查询
- `template/` — 4 种机柜模板（喷淋稳压泵/排烟风机/正压送风风机/消火栓稳压泵），各有 standard.jpg + config.json
- `images/` — 4 张机柜参考图，Phase 2 降级时发给 VLM 做视觉对比

### 对话窗口「讲解视频」（agent_video，纯加法）

与表单页 Video 模块**完全独立**（独立模块 `service/agent_video/`、集合 `agent_video_tasks`、
路由 `/api/v1/chat/agent-video/*`、开关 `VIDEO_AGENT_ENABLED`）：

- 链路：PPT →（soffice）PDF →（pypdfium2）逐页 PNG → 逐页解说词（`model_gateway.chat`）
  → 逐页 TTS（复用 `service/video/tts.py` + **同一把** Redis 配额 `rz:video:tts:slots`）
  →（ffmpeg）每页片段 → concat
- **每页产物落库** → 改一页只重跑该页（`video_update_page`）、换语言后 `video_rerender` 生效
- **多页改文案用 `video_update_pages`**（一次传完）：逐页调会被 LoopDetector 的"同一工具累计 20 次"
  中止本轮（`TOOL_FREQ_HARD`，见 `core/agent/middlewares/loop_detect.py`），且每页重 concat 一次整条
- **生成前先确认（用户要求）**：模型先报一条方案请用户确认（只问用户没交代的），确认项限平台真有能力：
  主题/画幅/配音（语言+音色）/字幕（`show_subtitles`、`subtitle_language`）/目标时长（`target_minutes`，按实测语速
  中文≈320 字·分、英文≈1040 字符·分折算每页字数上限，**只能近似**）/其它要求（`requirements`）；
  **不许承诺**背景音乐、转场、局部放大、数字人、人物对白。成片后 `video_status` 的 `duration_seconds` 如实报实际时长
- **生成类工具阻塞到任务终态**（`service.wait_for_task`，上限 `VIDEO_AGENT_WAIT_TIMEOUT`）：返回里带
  签名播放链接，模型当场输出 `<file>`，视频与本次请求一一对应；等待期间每 4s 推一次 SSE `status`
  进度（同时避免流因静默被看门狗/nginx 掐断）。超时不取消任务，返回 `finished=false` 交前端补卡片
- **字幕默认烧进画面**（`subtitles.py`，`VIDEO_AGENT_SUBTITLE`）：解说词按标点切条、按字数占比
  分时长，用 ffmpeg `drawtext` 烧在底部（字走 `fontfile=RZ_CJK_FONT`、文本走 `textfile=` 免转义、
  折行按宽度而非字符数，中英通用；**白字 + 极细深色描边 `borderw`，字号 `0.030h`** ——
  不用半透明黑底框：白底页会看不见字，所以留描边而不是"框"）。不用 libass（本机 ffmpeg 没编），
  也不做软字幕轨（软轨没法增量重编）
- **配音/字幕语言可分开（仅中/英）**：`narration.speech_language()` 归一成 zh-CN/en-US 传给
  `synthesize_speech`（TTS **不翻译**，文案语言必须与之一致）；`subtitle_language` 与配音语言不同时，
  一次调用产出 `{speech, subtitle}` JSON（`needs_dual`）；同语言走原单语路径
- **RITL 自检**：Pillow 判空白/全黑页 + `model_gateway.vision()` 看 contact sheet（截断/缺失/乱码）
- **可靠性**：解说词并发 ≤3（LLM 网关限流）、重试 3 次（退避+抖动）、单页失败降级不整单失败、
  全页失败才报错。TTS 用**独立并发** `VIDEO_AGENT_TTS_CONCURRENCY=8`（上游配额是 64）；合成是
  **逐页串行 ffmpeg**，50 页 ≈135s，是主要耗时项之一
- **分阶段耗时**：跑完/失败都打 `slides=… ritl=… narration=… tts=… compose=…`（`pipeline._Stages`），
  合成分另记平均与最慢 3 页——定位瓶颈只能靠它
- **必须后端进程跑 soffice/ffmpeg**（沙箱禁子进程，见 `core/sandbox/runner.py`）
- 工具在 `tools/agent_video_tools.py`，权限 `ai:video`（工具内自查）；镜像依赖
  `libreoffice-impress`/`fonts-noto-cjk`/`fontconfig`/`ffmpeg`/`pypdfium2`，预检
  `deploy/scripts/media_preflight.sh`（容器里跑：`docker exec -i rz-backend bash -s < 该脚本`）
- **中文字体坑**：soffice 找不到中文字体不会报错，只把汉字渲染成空白（实测事故）。判据是
  「LibreOffice 找得到」而非「系统里装了」。两条来源：PPT 渲染走 fontconfig（容器靠
  `fonts-noto-cjk` + 发行版配置，**代码不注入**；仅系统无配置的 macOS 开发机由 `slides.py`
  的 `_fontconfig_file()` 补一份含 `<include>` 的配置）；Pillow/matplotlib/WeasyPrint 走
  `settings.RZ_CJK_FONT` 文件路径（`assets/fonts/wqy-microhei.ttc`，**必须留在 git 里**，
  否则全新 clone/CI 构建静默丢汉字）。闸门：`cjk_render_available()` 探针（建任务时判定）、
  `text_pages.require_font()`、`runner.py` 注入前校验；预检第 3/4/5 项同标准，烟测**不再兜底**

### Video 模块 (PPT → MP4)

**四阶段流水线（插件化架构）：**
```
上传 PPT → Step 1: python-pptx 提取文字
         → Step 2: LLM 插件生成解说词 (:8010)
         → Step 3: 图片转换 (:8020) + TTS 语音合成 (并行)
         → Step 4: 视频渲染插件合成 MP4 (:8030)
```

- `service/video/pipeline.py` — 流水线编排，异步执行，状态持久化到 MongoDB `video_tasks`
- `service/video/tts.py` — TTS 适配器，支持 qwen / openai 两种后端
- **TTS 并发配额必须跨进程** — 上游 `tts_proxy` 硬限同时 2 个请求，而生产是 `--workers 4`：进程内的 `asyncio.Semaphore` 最多只限住 1/4 的并发。故由 `service/video/pipeline.py` 用 Redis `DistributedSemaphore`（复用 `core/scheduler/semaphore.py`，key `rz:video:tts:slots`）统一限流，配额取 `TTS_MAX_CONCURRENCY`
- **「忙」不等于「坏」**（2026-09-20 事故）— `_acquire_tts_slot` 原实现把两者混为一谈：等 `TTS_SLOT_WAIT_TIMEOUT` 秒拿不到槽位就 fail-open 放行。但 50 页 PPT 的 TTS 队列排空要 ≈1250s（单段 ≈50s ÷ 配额 2），60s 时几十个等待者会**成批**把请求发出去，上游瞬间看到远超 2 的并发 → 429 成灾。**只有 Redis 不可用才 fail-open；「槽位被占」是正常排队，必须等下去**（超时默认 600s）。配套两条：① 本进程在飞上限 `TTS_LOCAL_CONCURRENCY`（**进程级**，见 `_get_tts_local_slots`；写成每次流水线各建一个就等于没限）压住等待者数量；② `_touch_slot_ttl` 给计数器 `EXPIRE TTS_SLOT_LEASE_TTL` 续租约，worker 被 kill 漏掉的槽位最迟该时间自愈——这正是 fail-open 能收紧的前提
- **TTS 429 判据要认代理的包装** — 上游限流返回 429，但 `tts_proxy` 把它包成 **502**（body 里含 `Upstream TTS HTTP 429`）。重试判据只写 `status_code == 429` 会让退避重试完全失效（每次限流第一枪就死）。退避基数 `QWEN_TTS_RETRY_BASE_DELAY=5`（→5/10/20s）：原 1/2/4s 合计 7s，比一次槽位周转（≈25s）还短，等于没退
- **传输层异常也要走重试** — `await client.post()` 抛的 `httpx.TransportError`（`ReadTimeout`/`ConnectError` 等）原先没有 try 包住，会直接冲出整个重试循环：重试只认 HTTP 状态码，一次网络抖动就整段报废、进而整本视频报废（`gather` 无 `return_exceptions`）。现与 429 同属可重试（注意 `TransportError` 不含 `HTTPStatusError`，`raise_for_status` 行为不变）。配套 `TTS_REQUEST_TIMEOUT=300`：单段 ≈50s，两槽都忙时还要再等一次周转 ≈50s，合计 ≈100s，原 120s 余量太薄
- `api/v1/video.py` — CRUD 端点 (`POST /video/tasks`, `GET /video/tasks`, `DELETE /video/tasks/{id}`)，权限 `ai:video`
- `models/video.py` — VideoTask/VideoTaskPublic/VideoTaskStatus DTO
- 配置项：`PLUGIN_LLM_GENERATOR_URL`, `PLUGIN_IMAGE_CONVERTER_URL`, `PLUGIN_VIDEO_RENDERER_URL`, `TTS_PROVIDER`, `TTS_MAX_CONCURRENCY`, `TTS_LOCAL_CONCURRENCY`, `TTS_REQUEST_TIMEOUT`, `TTS_SLOT_WAIT_TIMEOUT`, `TTS_SLOT_LEASE_TTL`

### 预置权限

| 角色 | 权限 |
|------|------|
| `root` | 所有权限 |
| `rag_basic` (新用户默认) | `rag:chat` |
| `rag_admin` | `rag:chat/upload/delete/export` |
| `risk_user` | `risk:check/batch_check/report` |
| `risk_expert` | `risk:check/batch_check/fire_safety/report` |
| `query_user` | `query:platform/access` (Agent 平台工具访问) |
| — | `ai:agent` (Agent 对话 + 沙箱产物下载) |
| — | `ai:video` (视频生成) |

### 外部系统

- DCIM / 综合管理平台(mgmt) / 安防平台(security) — Agent 平台工具后端。mgmt 走**认证中心**：用户绑定自己的 mgmt 账号密码（Fernet 加密落库，`core/mgmt_credentials.py`，密钥由 `JWT_PRIVATE_KEY` 派生）后按用户 token 查询，mgmt 侧数据权限按人生效；未绑定用户默认用「rz 手机号 + `MGMT_DEFAULT_PASSWORD`」登录，登录失败提示去【认证中心】绑定。登录接口 `/user/login/v1`，token + ownDeptId + companyId 按 user_id 缓存 Redis，请求带 `token` + `Deptid` 头。DCIM / security 仍走服务账号 token
- 飞书 IM 机器人（`LARK_ENABLED`）— `service/feishu/`，长连接订阅 → Agent 复用
- 微信 IM 机器人（`WECHAT_ENABLED`，个人微信 iLink/ClawBot）— `service/wechat/`：HTTP/JSON 长轮询（`POST /ilink/bot/getupdates`，hold ≈35s）→ Agent 复用。**协议约束**：不能编辑已发消息、也无打字态（无流式，累计到终稿一次发）、不能主动推（须回带 `context_token`）、游标单消费（必须选主 `rz:wechat:leader`）、**不能读微信群消息**（仅私聊 p2p）。`errcode -14` = bot_token 过期。**接入（扫码拿凭据，运维一次性动作）走命令行脚本 `deploy/scripts/wechat_login.py`**：调 iLink 取码（GET `get_bot_qrcode`/`get_qrcode_status`）→ 二维码存 `wechat_qrcode.png` → 手机扫码 → 凭据落盘 `WECHAT_STATE_FILE` → 重启后端装载。绑定/产物端点见 `api/v1/wechat.py`
- 通道公共件（`service/channels/`）— 飞书/微信共用的通道无关逻辑：`connect_code.py`（一次性绑定码）、`artifact_token.py`（签名下载 token）、`leader.py`（`LeaderLock` 选主）、`file_tags.py`（标签降级）、`agent_stream.py`（SSE 消费）。飞书侧为薄封装（签名不变，零回归）
- 工单服务（`ticket-api.ddbes.com`，`MGMT_TICKET_BASE_URL`，mgmt 能力延伸，非新平台）— 待办/变更/故障/问题/事件工单后端。**2026-10-26 对方整体重做**：路径去掉 `/ticket`、`/pen` 前缀（`/wiporder/*`、`/pending/list/v1`、`/company/user/{uid}`），认证换成 mgmt 同一套 `token` + `Deptid`（`http.ticket_request()`，401 清缓存重登一次），入参与信封不变，`type` 必填（老代码默认 `type:1`）。老 host 与 `pen_request()` 保留未删但不再被调用。变更工单按公司（`POST /wiporder/ticketlist/modify`，companyId 认 kingdee 格式，跨公司经 `mgmt.query_company_list`（`/authority/userrole/v1/current/company`）映射；已是 kingdee 的入参直接透传，不查映射）；各类工单详情 `GET /wiporder/query/breakdown/{id}` / `/wiporder/question/{id}` / `/wiporder/event/{id}`（id 取列表返回的 id，端点按工单类型区分）；我的待办 `/pending/list/v1` 直接按 mgmt token 归属人查，不再换发人员 token。迁移记录见 `docs/problems/pen-ticket-api-migration.md`
- **红外巡检图片质量检测（`INFRARED_QUALITY_ENABLED`，默认关）** — `service/infrared_quality/`，卡片 +
  整屏看板挂在**不合规检查页**。判定不用 VLM，调外部 Gradio 原始 REST（upload → call → SSE
  `event: complete`，不引 `gradio_client`；**Gradio 6 起文件入参须是带 `meta._type` 的 `FileData`
  对象，裸路径被拒**），结果表 `{headers:["文件名","Prediction","sharp","borderline","defocused","错误"]}`
  按**表头名**定位列（无表头才回退旧位置），按文件名（同名按位置）映射回设备；概率列是带 `%`
  的字符串（未校准 softmax），「模型分数」取预测类别那一列的原值。**检测单位是用户勾选的
  巡检任务**（每月只更新部分任务，查过的不重复查）：mgmt `/infrared/inspection/task/list|detail/v1/…`
  取任务与设备，`picture` 是 7 天预签名 URL → **检测时顺手把图留在本地**
  （`INFRARED_QUALITY_IMAGE_DIR/{task_id}/{设备id}{ext}`；**缓存命中的设备也补图**，本地已有则跳过 →
  老任务重跑一次检测即可补齐证据，不必勾「重新推理」），明细的 `图片链接` 改写成
  `GET /scheduled-tasks/infrared-quality/image?task_id=&name=`（老记录无本地文件才回退原链接），
  前端按图片渲染（`RecordsTable` 的 `imageColumns`，端点要 Bearer → axios 拉 blob）；
  **`status=1` 进行中任务不可勾**（无时间/图片）。
  **详情设备是台账、远多于本次巡检台数**（2026-11-09：`2EsTvRH1SPC` 列表 36 台、详情 553 台）→
  **只对有 `picture` 的设备出行**，`device_count` 取详情 `deviceCount`、另存 `photo_count`；整任务无图 →
  `result_status='no_image'`（明细为空、计数全 0、照常写 `checked_at`）。**旧文档里的「说明=无图片」行
  读时过滤、不删库**（`records` 丢弃，`list_checked` Mongo 聚合重算计数并兜底推 `photo_count`），旧任务不用重跑。
  **判定 fail-closed**：sharp→合格、borderline→待复核、defocused→不合格，其余（uncertain/未知/空/
  下载失败/推理异常）一律「检测失败」。**只认新英文标签**（2026-11-10 接口改版）：旧中文输出属未识别，
  缓存命中的旧任务勾「重新推理」重跑即可（原「36 张图 0 合格」的模型侧问题按实测已不复现，
  见 `docs/problems/infrared-quality-no-clear-label.md`）。降级：预检
  （`GET /config`）不可达→整轮 failed；单任务详情失败→该任务 failed **且保留上次明细**；连续 3 批推理失败→
  剩余「检测服务中断」、任务 partial。存储：一任务一文档（自带 rows，重检整条覆盖）+ 批次索引 + 预测缓存
  （**只存模型原始标签**；存量旧中文标签会判未识别，勾「重新推理」重跑覆盖；写入必须用
  `pymongo.UpdateOne`，mongosh 风格 dict 会让 `bulk_write` 报 `is not a valid request`）。租约 `infrared_quality:sync`
  （300s / 30s 心跳，Redis 挂 fail-open）。端点
  `GET /scheduled-tasks/infrared-quality/tasks`、`POST .../check`、`GET .../records`、
  `GET .../image`。本机 dry-run
  `python -m service.infrared_quality.cli --dept A1 --company 廊坊`、单图复测 `--predict <file>`、
  自检 `python -m service.infrared_quality.tests.test_infrared_quality`（24 项）。详见 `docs/infrared-quality-check.md`
- skills — 纯本地文档能力（`skills/` 目录），无外部 API 依赖；用户自建技能运行期写入 `skills/users/`
- Coze Studio — 独立 Go 服务，后台 API 代理调用 (配置了但 `service/coze_proxy/` 未实现)
- LLM — OpenAI 兼容 API，视觉模型默认 `qwen3.5-vl`，对话模型默认 `qwen3.5`
- Milvus — 向量数据库，default: `http://localhost:19530`
- MongoDB — `rz_ai_platform` database，存储 users/roles/applications/audit_logs/video_tasks/feedbacks
- Redis — Session/Token 黑名单/缓存/调度器，prefix `rz:`
- MinerU — PDF 解析服务 (mineru.net API)，`MINERU_ENABLED=true` 时启用

### 已知未完成模块

- `service/coze_proxy/` — 仅空 `__init__.py`，Coze Studio 代理未实现
- 数据分析沙箱硬化 — 当前为子进程 + 资源限制的 MVP，计划演进为 Docker 容器 + Seccomp（对齐 DifySandbox）

### 前端

React + TypeScript + Vite + Tailwind CSS 4 + shadcn/ui。入口 `ChatPage.tsx`，`platform-shell.tsx` 侧边栏展示功能入口（知识库/隐患检测/消防配置/视频生成/权限中心/Agent）。

**页面：** `LoginPage`, `ChatPage` (RAG 对话 + Agent 智能体), `KnowledgePage` (知识库管理), `RiskPage` (隐患检测), `FireSafetyPage` (消防配置), `VideoPage` (视频生成), `PermissionPage` (权限申请/审批), `OverviewPage` (仪表盘), `InnovationPage` (创新工坊), `SettingsPage` (设置), `admin/` (管理后台)

**Agent 相关组件：** `ChatArea.tsx` (SSE 对话流 + 工具卡片渲染 + 右侧看板面板), `ChatInput.tsx`, `ChartRenderer.tsx` (Agent `<chart>` 标签 26 种图表本地渲染), `MermaidRenderer.tsx` (`<mermaid>` 标签), `FileCard.tsx` (沙箱产物下载卡片，`.html` 加「在看板中查看」), `ArtifactPanel.tsx` (右侧 HTML 看板面板), `HtmlPreview.tsx` (sandbox iframe 预览，echarts 打包内联)

**状态管理：** Zustand — `authStore`, `chatStore`, `appStore`, `videoStore`

**API：** Axios client (`web/src/api/client.ts`)，按模块拆分 `auth.ts`, `chat.ts`, `rag.ts`, `risk.ts`, `video.ts`, `admin.ts`, `stats.ts`

### AI 协作规范

- **新建文件必须 `git add`**：AI 生成的新文件（`??` 状态）默认不进入版本控制，提交前需执行 `git add <新文件或目录>`。提交时检查 Commit 窗口的 Unversioned Files 分组，确保新代码不被遗漏。
