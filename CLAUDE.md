# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 常用命令

```bash
# 安装后端依赖（遇 SSL 证书问题加 --trusted-host pypi.org --trusted-host files.pythonhosted.org）
pip install -r requirements.txt

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
- **工具集**：基础工具 `[rag_knowledge_search, get_current_time, send_notification]`；`AGENT_ANALYSIS_ENABLED` 时追加分析工具 `build_analysis_tools()`；任一 `<PLATFORM>_ENABLED` 开启时把已注册平台能力生成为平台工具（deferred，见下）
- **middleware 链**（`core/agent/middlewares/`）：`DurableContextMiddleware`（注入摘要+工具结果，防压缩丢失）→ `SummarizationMiddleware`（token 超阈值压缩旧消息）→ `DeferredToolFilterMiddleware`（海量工具 schema 隐藏/按需 promote）。另有 `audit.py` / `guardrail.py` / `loop_detect.py` / `rag_inject.py` / `tool_error.py`
- **Checkpointer** — `core/agent/checkpointer.py` `create_checkpointer()`：`AGENT_CHECKPOINT_BACKEND` 三选（memory 默认 / postgres），postgres 用 `AsyncPostgresSaver` + 连接池，进程退出前 `teardown()`
- **摘要** — `core/agent/summarization.py`：`AGENT_SUMMARIZE_ENABLED`、`AGENT_SUMMARIZE_TRIGGER`（如 `tokens:32000`）、`AGENT_SUMMARIZE_KEEP`

**意图路由与工具选择（promote 机制）：**
```
用户消息 → core/agent/query_planner.py (LLM 意图 → IntentPlan: single/parallel/detect/chain)
        → core/agent/orchestrator.py build_initial_state/route_to_tools (按绑定表 promote 工具组)
        → core/agent/deferred.py DeferredToolFilterMiddleware (隐藏未 promote 工具 schema)
        → 未命中时 LLM 用 tool_search 语义检索 promote 所需工具
```
- 能力集稳定前 `intent_bindings.all_intent_labels()` 为空时跳过编排（省一次白付的 LLM 调用）
- 并行/串行工具调用由 LangGraph ToolNode 执行，调度方式（单/多/串行/并行）由 LLM 自主决定

**平台能力注册（四平台，见「平台能力」节）：**
```
service/query/bootstrap.py register_enabled_platforms()
  → DCIM_ENABLED/MGMT_ENABLED/SECURITY_ENABLED 开关注册 + skills 平台无开关常驻
  → base/manifest.py build_category_manifest() 生成 <available-tools> 分类清单
  → base/tools_factory.py build_all_tools() 生成为 StructuredTool（schema 强校验 + 输出截断
    AGENT_MAX_TOOL_OUTPUT_CHARS，记录级 pop + 字符级兜底）
```

**分析出图 / 报告（见「数据分析沙箱」节）：** `analysis_load` 物化全量数据 → `analysis_exec` 沙箱跑 pandas 聚合 → 返回 `chart_data` 组装 `<chart>` 标签（前端 ChartRenderer.tsx 本地渲染，不出内网）；产物文件回传 `artifacts`，组装 `<file url name>` 标签供下载。用户要「看板/大屏」时走 **HTML 看板**路径：`analysis_write_file` 写自包含 `.html`（数据内联 + echarts）→ `analysis_exec(file=...)` → `<file>` 标签 → 前端 `ArtifactPanel.tsx`/`HtmlPreview.tsx` sandbox iframe 本地预览（echarts 打包内联，不出内网）。

### 平台能力（DCIM / 综合管理 / 安防 / 技能）

统一检索层 `service/query/base/`（`platform.py` Platform 注册、`capability.py` Capability、`tools_factory.py` 工具生成、`retriever.py` 检索、`manifest.py` 分类清单、`intent_bindings.py` 绑定表）。每个平台一套 `capabilities.py`（能力声明）+ `endpoints.py`（EndpointSpec 接口声明）+ `client.py`（通用调用器）三件套，params 用 snake_case、经 `param_map` 映射为接口 camelCase。

| 平台 | 能力规模 | 开关 | 说明 |
|------|---------|------|------|
| DCIM 数据中心 | 6 条 | `DCIM_ENABLED` | 告警/资产/容量/用电：`query_alarm`, `query_alarm_history`, `query_asset`, `query_asset_count`, `query_capacity`, `query_power_usage` |
| mgmt 综合管理平台 | 38 条 | `MGMT_ENABLED` | 人员(org)/巡检(inspection)/维护(maintenance)/设备(device)/文档(document)/工单(work_order)/配置(config) 分类，如按姓名查人、设备台账、设备维护计划、设备类型树、巡检项模板、红外温度检测、巡检工单、待办工单、变更工单、故障/问题/事件工单详情、配置管理、任务列表、标准规范等 |
| security 安防平台 | 0 条 | `SECURITY_ENABLED` | 占位空元组，接口文档到位后登记 |
| skill 技能平台 | chart 1+26、html_dashboard 1+1、skill_creator 1+0（另有用户自建技能动态增减） | 无开关常驻 | `skills/` 目录自动发现（SKILL.md + references），零代码接入新技能；用户自建技能存 `skills/users/<uid>/`，按用户隔离 |

- 默认开关全 False，Agent 只保留知识检索/时间/通知基础工具
- **用户自建技能（复刻 DeerFlow skill-creator）**：`skill_manage` 工具（受 `ai:agent` 门控）让用户对话创建/查看/修改技能，按用户隔离存 `skills/users/<uid>/<name>/`（公开技能在 `skills/<name>/`）；技能名全局唯一，写盘过静态+LLM 语义安全扫描（`AGENT_SKILL_LLM_SCAN`，fail-closed）。`skill_creator` 技能引导访谈→起草→创建。热加载：写盘后 `bump_skills_version()` → 下一条消息触发 agent 重建。用户技能不进 `<available-tools>`/路由词汇表，按需 tool_search 命中。**部署注意：`skills/users/` 是运行期写入，Docker 需挂 volume，否则容器重建丢技能**
- mgmt 能力详情见 `service/query/mgmt/capabilities.py` 顶部 docstring；EndpointSpec 字段（method/path/param_map/defaults/default_params/result_path/page_meta/value_map 等）见 `service/query/mgmt/endpoints.py`
- 已实测接口的坑：URL 路径含 "get"（如 `/equipment/get/dept`）实际是 **POST**（GET 返 405）；path 占位符参数必须必填（defaults 只兜底 query/body 参数）；MyBatis-Plus 分页信封 `records/total/current/pages/size` 用 `page_style='page_size_alt'`

### 数据分析沙箱

LLM 需要做全量/统计/聚合/报告时，数据先物化到沙箱（原始记录不进上下文），再在受限子进程跑 pandas。

- `tools/analysis_tools.py` — `analysis_load`（按平台能力分页拉**全量**数据落盘，只回 dataset_id/rows/columns/预览，受 `AGENT_ANALYSIS_MAX_ROWS` 保护）+ `analysis_exec`（跑 pandas，`result` 变量 → `chart_data`；产物文件 → `artifacts`）；`tools/analysis_file_tools.py` 提供 `analysis_write_file`/`analysis_str_replace`/`analysis_exec(file=...)` 脚本化写报告
- `core/sandbox/datasets.py` — 数据集落盘 `{AGENT_ANALYSIS_DATA_DIR}/{thread_id}/{dataset_id}/data.csv + meta.json`，按会话隔离，超 `AGENT_ANALYSIS_MAX_DATASETS_PER_SESSION` 删最旧
- `core/sandbox/runner.py` — `run_python()` 隔离执行：`python -I` 子进程、RLIMIT_AS 内存 + RLIMIT_CPU、socket/urllib 置空禁网络（保留可被 ssl 子类化）、subprocess/os.system/os.fork/exec*/pty 置空禁子进程、外部 `asyncio.wait_for` 超时；产物扫描剔除 data.csv/meta.json/_chart_data.json/.py，文档产物剔除冗余图片
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

### Video 模块 (PPT → MP4)

**四阶段流水线（插件化架构）：**
```
上传 PPT → Step 1: python-pptx 提取文字
         → Step 2: LLM 插件生成解说词 (:8010)
         → Step 3: 图片转换 (:8020) + TTS 语音合成 (并行)
         → Step 4: 视频渲染插件合成 MP4 (:8030)
```

- `service/video/pipeline.py` — 流水线编排，异步执行，状态持久化到 MongoDB `video_tasks`
- `service/video/tts.py` — TTS 适配器，支持 edge (本地免费) / qwen / openai 三种后端
- `api/v1/video.py` — CRUD 端点 (`POST /video/tasks`, `GET /video/tasks`, `DELETE /video/tasks/{id}`)，权限 `ai:video`
- `models/video.py` — VideoTask/VideoTaskPublic/VideoTaskStatus DTO
- 配置项：`PLUGIN_LLM_GENERATOR_URL`, `PLUGIN_IMAGE_CONVERTER_URL`, `PLUGIN_VIDEO_RENDERER_URL`, `TTS_PROVIDER`

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

- DCIM / 综合管理平台(mgmt) / 安防平台(security) — Agent 平台工具后端，走服务账号登录（`/center/user/login/v1`，Redis 缓存 token + ownDeptId + companyId），请求带 `token` + `Deptid` 头
- pen 流程服务（`api.ddbes.com`，mgmt 能力延伸，非新平台）— 待办/变更/故障/问题/事件工单后端，`Authorization: Bearer` 认证。待办按人（token 归属人，内部用服务账号换发指定人员 token，`/user/dandang/user/token/{kingdeeUid}`，path 参数区分人）；变更工单按公司（`POST /ticket/wiporder/ticketlist/modify`，companyId 认 kingdee 格式，跨公司经 `/pen/pending/list/company` 映射）；各类工单详情 `GET /ticket/wiporder/query/breakdown/{id}` / `/ticket/wiporder/question/{id}` / `/ticket/wiporder/event/{id}`（id 取待办列表返回的 id，任意有效 token 可查，端点按工单类型区分）。token 换发接口不暴露为 LLM 工具
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

**Agent 相关组件：** `ChatArea.tsx` (SSE 对话流 + 工具卡片渲染 + 右侧看板面板，裸产物 URL / Markdown 链接自动转文件卡片), `ChatInput.tsx`, `ChartRenderer.tsx` (Agent `<chart>` 标签 26 种图表本地渲染), `MermaidRenderer.tsx` (`<mermaid>` 标签), `FileCard.tsx` (沙箱产物下载卡片，`.html` 加「在看板中查看」，`.pdf` 加「PDF 预览」), `ArtifactPanel.tsx` (右侧 HTML 看板面板), `HtmlPreview.tsx` (sandbox iframe 预览，echarts 打包内联)

**状态管理：** Zustand — `authStore`, `chatStore`, `appStore`, `videoStore`

**API：** Axios client (`web/src/api/client.ts`)，按模块拆分 `auth.ts`, `chat.ts`, `rag.ts`, `risk.ts`, `video.ts`, `admin.ts`, `stats.ts`

### AI 协作规范

- **新建文件必须 `git add`**：AI 生成的新文件（`??` 状态）默认不进入版本控制，提交前需执行 `git add <新文件或目录>`。提交时检查 Commit 窗口的 Unversioned Files 分组，确保新代码不被遗漏。
