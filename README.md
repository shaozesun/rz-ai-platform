# 润泽 AI 平台（rz-ai-platform）

数据中心统一 ToB AI 服务——面向运维工程师的**对话式智能运维助手**。

## 项目背景与定位

平台由两个项目合并演进而来：

- **risk-api** — 安全隐患检测（图片上传 → 消防控制柜状态识别 → 安全报告）
- **rz-rag** — RAG 知识库问答（企业文档检索增强生成）

在此之上进一步演进出了 **Agent 智能体平台**：Agent 能按用户问题自主规划意图、调用平台工具获取实时运维数据（告警/设备台账/维护计划/人员信息等）、把全量数据物化到沙箱做统计分析、生成图表与报告文件，让运维工程师用自然语言完成「查数据 → 分析 → 出结论/报告」的完整链路。

平台另有若干**确定性流水线**（不依赖模型自由发挥）：周期性的五类工单分析报告、告警不合规班次检查、月度文档更新检查、红外巡检图片质检、数据中心事件统计表；对外通过 Web 页面、飞书机器人与微信机器人三个入口提供服务。

目标用户场景：运维工程师日常监控、故障排查、知识查询、工单与值班通知。

## 技术栈

| 层 | 技术 |
|----|------|
| 后端 | Python 3.11 · FastAPI · LangGraph/LangChain |
| Agent | LangGraph（图执行/Checkpointer/流式）· 结构化输出意图路由 · 受限子进程数据分析沙箱 |
| 存储 | MongoDB · Redis · Milvus（向量库）· PostgreSQL（Checkpointer / DCIM 元数据）· TDengine（DCIM 时序） |
| 数据接入 | Kafka（DCIM 动环实时与告警）· 飞书长连接 · 微信 iLink 长轮询 |
| LLM | OpenAI 兼容 API（对话 `qwen3.5` / 视觉 `qwen3.5-vl`）；红外质检走外部 Gradio 服务 |
| 前端 | React · TypeScript · Vite · Tailwind CSS 4 · shadcn/ui · Zustand |
| 部署 | Docker Compose（nginx + MongoDB + Redis + Milvus + PostgreSQL + TDengine + browser + Loki + Promtail + Grafana） |

## 快速开始

```bash
# 后端
pip install -r requirements.txt
python main.py                     # 或 uvicorn main:app --reload --host 0.0.0.0 --port 8000

# 前端（另开终端）
cd web && npm install && npm run dev   # Vite 开发服务器 (localhost:5173)

# Docker Compose 部署
cd deploy && docker compose up -d
```

关键开关（`.env`，功能开关默认关闭）：

| 类别 | 开关 |
|------|------|
| Agent | `AGENT_ENABLED`（对话入口）· `AGENT_ANALYSIS_ENABLED`（数据分析沙箱）· `VIDEO_AGENT_ENABLED`（讲解视频） |
| 平台工具 | `MGMT_ENABLED`（综合管理 54 条）· `DCIM_ENABLED`（动环监控 9 条）· `SECURITY_ENABLED`（占位）· `WEB_ENABLED`（联网检索/抓取） |
| 页面截图 | `BROWSER_ENABLED`（按配方截图）· `BROWSER_EXPLORE_ENABLED`（交互浏览：打开/点击/读文本/录像） |
| 定时与检查 | `SCHEDULED_TASKS_ENABLED`（定时任务 + 分析报告预设）· `COMPLIANCE_ENABLED`（告警不合规检查）· `DOC_UPDATE_CHECK_ENABLED`（文档更新检查）· `INFRARED_QUALITY_ENABLED`（红外图片质检）· `INCIDENT_STATS_ENABLED`（事件统计表） |
| 数据接入 | `DCIM_KAFKA_ENABLED`（Kafka worker 总开关，另有实时/告警/工单三个独立开关）· `LARK_ENABLED`（飞书机器人）· `FEISHU_GROUP_INGEST_ENABLED`（园区群消息入库）· `WECHAT_ENABLED`（微信机器人） |

静态检查与自检（仓库未装 pytest，自检均为纯 assert + 非零退出码，秒级完成）：

```bash
# 专抓「名字未定义」这类只有真调用才炸的错（需 pip install pyflakes）
python -m pyflakes api core service tools repository models config main.py 2>&1 | grep "undefined name"

# 前端
cd web && npm run lint && npm run build

# 典型自检（改到对应模块时跑）
python -m service.scheduled_task.report.tests.test_report_spec
python -m service.scheduled_task.compliance.tests.test_compliance
python -m service.infrared_quality.tests.test_infrared_quality
python -m service.dcim_ingest.tests.test_shard_columns
```

## 目录结构

```
api/v1/      →  FastAPI 路由（auth/rag/chat/risk/video/agent/agent_video/admin/stats/
                feedback/health/scheduled_tasks/skills/tools/feishu/wechat）
service/     →  业务逻辑
                rag/ risk/ auth/ video/ agent/ agent_video/ query/（平台能力检索层）
                scheduled_task/  定时任务 + 五类确定性报告 + 事件统计 + 告警不合规检查
                doc_update/      月度文档更新检查
                infrared_quality/ 红外巡检图片质量检测
                dcim_ingest/     DCIM Kafka 接入与落库
                channels/        飞书/微信通道公共件
                feishu/ wechat/  IM 机器人
core/        →  基础设施（model_gateway LLM 网关、auth_engine JWT、rbac 权限、
                agent/ LangGraph 图、sandbox/ 数据分析沙箱、scheduler/ LLM 调度器）
config/      →  配置与连接（settings / mongodb / redis / logger）
models/      →  Pydantic DTO
middleware/  →  ASGI 中间件（JWT 注入）
repository/  →  RAG 数据层（Milvus 向量库 / 文档加载 / 切分 / 稀疏编码）
tools/       →  Agent 工具（知识检索 / 时间 / 通知 / 沙箱分析 / 定时任务 / 事件统计 / 视频生成）
web/         →  前端（React + TS + Vite + Tailwind）
skills/      →  Agent 技能目录（chart、html_dashboard、deep_research 等，SKILL.md 自动发现；
                用户自建技能在 skills/users/<uid>/）
prompts/     →  LLM 提示词模板
docs/        →  架构设计、事故复盘与实现记录
deploy/      →  Docker Compose 部署 + browser 旁路容器 + 运维脚本
```

## 主要页面

| 分组 | 页面 |
|------|------|
| 对话 | `/assistant` 对话（RAG + Agent，含任务模式 / 计划模式 / 联网搜索开关） |
| AI 能力 | `/knowledge` 知识库 · `/risk` 隐患检测 · `/fire-safety` 消防配置 · `/video` 视频生成 |
| AI 能力 | `/scheduled-tasks` 定时任务 · `/analysis-reports` 分析报告（系统预设、共享只读） |
| AI 能力 | `/compliance` 不合规检查（班次检查 + 事件统计 + 文档更新 + 红外质检） |
| 智能体 | `/agent-skills` 技能中心（`/agent-tools`、`/agent` 重定向至此），工具目录在窗内打开 |
| 平台 | `/` 仪表盘 · `/innovation` 创新工坊 · `/permission` 权限申请/审批 · `/auth-center` 认证中心 · `/settings` 设置 · `/admin` 管理后台 |

## 核心实现架构链路

### 1. 认证鉴权链路

```
登录(手机号+密码+图形验证码) → auth_engine 签发 RSA JWT
  → AuthMiddleware 校验 Bearer token → 注入 request.state.user_id/permissions
  → @require_permission("resource:action") 从 MongoDB 加载角色权限 → 放行/403
```

新用户默认 `user` 角色（仅 `ai:chat`），其余能力需申请 → 审批通过后生效（也支持 mgmt 账号 SSO 登录，见下）。开发模式 `SMS_DEV_MODE=true` 跳过验证码、`PERMISSION_OPEN_MODE=true` 跳过 RBAC。

综合管理平台账号打通（`MGMT_SSO_ENABLED`）：`POST /auth/mgmt-login` 用 mgmt 账号密码登录并按手机号 find-or-create 本平台用户；普通注册时也会后台同步一次 mgmt 人员档案（姓名/公司/部门）。

### 2. RAG 问答链路

```
用户提问 → 文档切分入库 → Milvus BM25 稀疏 + Dense 混合检索
  → LLM 结合检索片段生成回答 → 流式返回前端
```

### 3. Agent 智能体链路（核心）

```
前端 ChatPage → ChatArea (SSE)
  → POST /api/v1/chat/agent（权限 ai:agent，thread_id=rz-agent-{session_id}）
  → AgentService.stream：设 RBAC contextvar → 按开关注册平台 → 构建 LangGraph 图
  → 意图路由：query_planner 识别意图 → orchestrator 按绑定表 promote 工具组
  → LangGraph 循环：LLM 决策 → 调用平台工具 / 沙箱分析 / tool_search 语义检索补工具
  → 结果流回前端 → <chart> 标签本地渲染图表、<file> 标签下载产物（.html 看板走右侧 ArtifactPanel iframe 预览）
```

- **平台能力注册**：`service/query/bootstrap.py` 按 `<PLATFORM>_ENABLED` 注册 mgmt（54 条）/ dcim_meta（9 条）/ security（占位）/ web（联网 2 条）/ browser（按配方截图，独立 Playwright 旁路容器）等平台 + skills 技能平台（无开关常驻，`skills/` 目录自动发现）。每平台 `capabilities.py`（能力声明）+ `endpoints.py`（EndpointSpec 接口）+ `client.py`（通用调用器）三件套；browser 平台用 `recipes.py` 声明式配方替代 endpoints。
- **promote 机制**：海量工具 schema 默认隐藏（`DeferredToolFilterMiddleware`），仅按意图预 promote 相关工具组；未命中时 LLM 用 `tool_search` 语义检索按需加载。
- **数据分析沙箱**：`analysis_load` 分页拉全量数据落盘（原始记录不进上下文）→ `analysis_exec` 在受限子进程跑 pandas（`python -I` + 内存/CPU 限制 + 禁网络/禁子进程 + 外部超时）→ `result` 变量产出 `chart_data`、文件产出 `artifacts`。
- **图表渲染**：LLM 输出 `<chart type="...">JSON</chart>`，前端 `ChartRenderer.tsx` 用 @ant-design/plots + @ant-design/graphs **本地渲染 26 种图表**，数据不出内网。
- **看板渲染**：用户要「看板/大屏」时，`skill.html_dashboard` 技能提供自包含 HTML 模板（数据内联 + ECharts），`analysis_write_file` 写入 → `analysis_exec(file=...)` 产出 `dashboard.html`，前端 `ArtifactPanel.tsx` + `HtmlPreview.tsx` 用 sandbox iframe + **打包内联 ECharts** 在右侧面板本地预览，数据不出内网。
- **长对话**：Checkpointer（memory/postgres）持久化消息 + SummarizationMiddleware 按 token 阈值自动压缩。

### 4. Risk 隐患检测链路（两阶段）

```
用户上传图片 → Phase 1 VLM 识别
  ├─ 非机柜场景 → 直接返回通用安全检测结果
  └─ 识别到 4 种消防控制柜 → Phase 2
      ├─ CV 检测：ORB(2000特征点) → FLANN LSH → RANSAC → LAB L 通道亮灭(阈值145)
      └─ CV 失败 → 降级 VLM 视觉对比（用户图 + 参考图）
→ Markdown / Word 双格式报告
```

### 5. Video 模块链路（PPT → MP4，四阶段插件化）

```
上传 PPT → python-pptx 提取文字
  → LLM 插件生成解说词 (:8010)
  → 图片转换 (:8020) + TTS 语音合成（并行）
  → 视频渲染插件合成 MP4 (:8030)
```

对话里另有一条独立的「讲解视频」能力（`service/agent_video/`，`VIDEO_AGENT_ENABLED`）：PPT 或纯文字大纲 → 逐页页图 → 逐页解说词 + TTS → 逐页 ffmpeg 片段（字幕烧进画面）→ 拼接 MP4，支持单页/批量改文案后的增量重跑。

### 6. LLM 调度器链路

```
LLM 调用 → TaskScheduler.schedule/schedule_stream
  → 优先级 = TaskType（业务场景）× RoleLevel（角色权重）
  → 非流式无槽位入队 Redis 优先级队列等待；流式只抢槽不排队（超时 503）
  → 抢到槽位执行 → 释放并通知等待者
```

Redis 不可用 / 调度器禁用时降级为本地信号量。

### 7. 定时任务与确定性报告

```
调度器（Redis 锁防多 worker 重复触发）
  → 到点调 AgentService().stream() 跑一轮（用户自定义指令）
  → 或走确定性流水线：service/scheduled_task/report/
       （固定 pandas 统计 + 固定 HTML 骨架 → 沙箱 WeasyPrint 出 PDF，统计阶段 0 次 LLM 调用）
```

- 五类工单报告（事件 / 故障 / 变更 / 问题 / 阈值）走**声明式规格**（`report/spec.py` 的 `ReportSpec` + 每类一份 `<ticket>_compute.py`），一条流水线、每类只换规格：统计与图两次运行逐字节一致，一份报告 80~95 秒、8 次模型调用。
- 系统预设全平台只跑一份、所有人看同一份（全国 + 各园区 × 周/月），挂在 `/analysis-reports` 页面；用户自定义任务按创建人隔离。
- 解读层（分章要点 / 图表解析 / 行动计划 / 执行摘要）有多道确定性闸：数值类的 `trend_guard` / `compare_guard` / `speculation_guard` / `meta_guard` 会丢句，`name_guard` 与「依据」校验只记日志。

### 8. 运营监控中心（`/compliance`）

不合规检查页聚合四类与班次无关的常态化检查，各自按周期产出 Excel / 交互表格：

| 检查 | 目录 | 口径 |
|------|------|------|
| 事件统计表 | `scheduled_task/incident_stats/` | 飞书园区群通报为入口，一行 = 一次已通报事件（速报 + 通报配对），按告警触发态时间精确同秒匹配 |
| 告警不合规检查 | `scheduled_task/compliance/` | 按班次核对四项：响应超时 / 未关单 / 未解除关单 / 重复告警 |
| 文档更新检查 | `doc_update/` | 25 项月度文档 × 各公司各部门，按期提交型看月份目录、原地更新型比 sha256 |
| 红外图片质检 | `infrared_quality/` | 按用户勾选的巡检任务逐张送外部 Gradio 模型，sharp/borderline/defocused → 合格/待复核/不合格，fail-closed |

### 9. DCIM 动环数据接入（Kafka → PostgreSQL + TDengine）

```
Kafka（配置类每小时全量重推 + 实时采样 + 告警/工单）
  → service/dcim_ingest/（多进程消费，按扁平序号轮询分片）
  → PostgreSQL（空间/设备/测点定义）+ TDengine（测点数值，宽表稀疏列写入）
  → service/query/dcim_meta/（PG 定范围 → TDengine 取值 → 应用层合并）
```

- 空间树最多 5 层（园区 → 数据中心 → 区域 → 楼层 → 房间），`space` 名称参数由服务端解析成节点后走递归子树，0 命中或多义一律报错回候选。
- 单表硬上限 4,096 列，超大设备类型按测点拆组（`st_<type>` / `st_<type>_g1…`），写侧按组各写一行、读侧按 `(ts, 设备)` 合并。
- 子表名带园区代号（`d_<园区>_<guid>`），因为设备 guid 只在园区内唯一且 TDengine 子表 tag 创建即固定。

### 10. IM 通道（飞书 / 微信）

```
飞书 WS 长连接 / 微信 HTTP 长轮询 → 通道解析 → 按 open_id / wechat_user_id 解析系统用户
  → 复用 AgentService().stream() → 按通道能力回投（飞书刷卡片；微信无流式，终稿一次性发送）
```

- 通道公共件在 `service/channels/`（一次性绑定码、签名下载 token、选主锁、`<file>/<chart>` 标签降级、SSE 消费）。
- 多 worker 靠 Redis 选主锁保证单消费：飞书 WS 不可重放但消息列表可回捞；微信游标单消费，失主即停。
- 园区群消息另有旁路入库（`FEISHU_GROUP_INGEST_ENABLED`），只入库不进对话，供事件统计表使用。

## 预置权限

权限点（`models/user.py` 的 `BUILTIN_PERMISSIONS`，共 8 个）：

| 权限 | 说明 |
|------|------|
| `ai:chat` | AI 对话 |
| `ai:agent` | Agent 对话、沙箱产物下载、分析报告页、不合规检查页、技能中心 |
| `ai:knowledge` | 知识库管理 |
| `ai:risk` | 隐患识别 |
| `ai:fire_safety` | 消防配置 |
| `ai:video` | 视频生成 |
| `scheduled_task:manage` | 定时任务管理 |
| `system:admin` | 系统管理（管理后台） |

预置角色：

| 角色 | 权限 |
|------|------|
| `admin` 管理员 | 全部 8 项 |
| `user` 普通用户（新用户默认） | `ai:chat` |
| `power_user` 全功能用户 | `ai:chat` + 知识库/隐患/消防/视频 + `ai:agent` |
| `dc_operator` 数据中心运维 | `ai:chat` + `ai:agent` + `ai:knowledge` |

## 外部系统

| 系统 | 用途 |
|------|------|
| LLM（OpenAI 兼容） | 对话/视觉/嵌入统一经 `core/model_gateway.py` |
| Milvus | RAG 向量库（BM25 稀疏 + Dense 混合检索） |
| MongoDB | 用户/角色/申请/审计日志/视频任务/反馈/定时任务/检查快照 |
| Redis | 会话/令牌黑名单/缓存/调度器/跨进程信号量与选主锁（prefix `rz:`） |
| PostgreSQL | Agent Checkpointer（可选）· DCIM 空间/设备/测点定义 · 事件统计原始消息 |
| TDengine | DCIM 测点时序（宽表 + 稀疏列写） · PUE 独立超级表 |
| Kafka | DCIM 配置/实时/告警/工单 topic |
| 综合管理平台（mgmt） | Agent 平台工具后端，支持认证中心按用户绑定 mgmt 账号（数据权限按人生效） |
| 安防平台（security） | 占位，接口文档到位后登记 |
| 工单服务（ticket-api） | 变更/故障/问题/事件/阈值工单列表与详情 |
| 飞书 / 微信 | IM 机器人入口（`LARK_ENABLED` / `WECHAT_ENABLED`） |
| 红外质检服务 | 外部 Gradio 模型，图片清晰度三分类 |
| MinerU | PDF 解析（`MINERU_ENABLED=true` 时启用） |
| Coze Studio | 后台 API 代理（已配置，`service/coze_proxy/` 未实现） |
