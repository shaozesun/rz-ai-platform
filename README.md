# 润泽 AI 平台（rz-ai-platform）

数据中心统一 ToB AI 服务——面向运维工程师的**对话式智能运维助手**。

## 项目背景与定位

平台由两个项目合并演进而来：

- **risk-api** — 安全隐患检测（图片上传 → 消防控制柜状态识别 → 安全报告）
- **rz-rag** — RAG 知识库问答（企业文档检索增强生成）

在此之上进一步演进出了 **Agent 智能体平台**：Agent 能按用户问题自主规划意图、调用平台工具获取实时运维数据（告警/设备台账/维护计划/人员信息等）、把全量数据物化到沙箱做统计分析、生成图表与报告文件，让运维工程师用自然语言完成「查数据 → 分析 → 出结论/报告」的完整链路。

目标用户场景：运维工程师日常监控、故障排查、知识查询、工单与值班通知。

## 技术栈

| 层 | 技术 |
|----|------|
| 后端 | Python 3.11 · FastAPI · LangGraph/LangChain |
| Agent | LangGraph（图执行/Checkpointer/流式）· 结构化输出意图路由 · 受限子进程数据分析沙箱 |
| 存储 | MongoDB · Redis · Milvus（向量库）· PostgreSQL（Agent Checkpointer 可选） |
| LLM | OpenAI 兼容 API（对话 `qwen3.5` / 视觉 `qwen3.5-vl`） |
| 前端 | React · TypeScript · Vite · Tailwind CSS 4 · shadcn/ui · Zustand |
| 部署 | Docker Compose（nginx + MongoDB + Redis + Milvus + Loki + Promtail + Grafana） |

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

关键开关（`.env`，默认全 False）：`AGENT_ENABLED`（Agent 对话入口）、`AGENT_ANALYSIS_ENABLED`（数据分析沙箱）、`MGMT_ENABLED` / `SECURITY_ENABLED` / `WEB_ENABLED` / `BROWSER_ENABLED`（平台工具与页面截图）、`BROWSER_EXPLORE_ENABLED`（页面交互浏览：打开/点击/读文本/截图）。

## 目录结构

```
api/v1/      →  FastAPI 路由（auth/rag/chat/risk/video/agent/admin/stats/feedback/health）
service/     →  业务逻辑（rag/ risk/ auth/ video/ agent/ query/）
core/        →  基础设施（model_gateway LLM 网关、auth_engine JWT、rbac 权限、
                agent/ LangGraph 图、sandbox/ 数据分析沙箱、scheduler/ LLM 调度器）
config/      →  配置与连接（settings / mongodb / redis / logger）
models/      →  Pydantic DTO
middleware/  →  ASGI 中间件（JWT 注入）
repository/  →  RAG 数据层（Milvus 向量库 / 文档加载 / 切分 / 稀疏编码）
tools/       →  Agent 工具（知识检索 / 时间 / 通知 / 数据分析沙箱）
web/         →  前端（React + TS + Vite + Tailwind）
skills/      →  Agent 技能目录（chart、html_dashboard 等，SKILL.md 自动发现）
prompts/     →  LLM 提示词模板
docs/        →  架构设计与实现记录
deploy/      →  Docker Compose 部署 + 备份恢复
```

## 核心实现架构链路

### 1. 认证鉴权链路

```
登录(手机号+密码+图形验证码) → auth_engine 签发 RSA JWT
  → AuthMiddleware 校验 Bearer token → 注入 request.state.user_id/permissions
  → @require_permission("resource:action") 从 MongoDB 加载角色权限 → 放行/403
```

新用户默认 `rag_basic`（仅 `rag:chat`），其余功能需申请 → Root 审批。开发模式 `SMS_DEV_MODE=true` 跳过验证码、`PERMISSION_OPEN_MODE=true` 跳过 RBAC。

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

- **平台能力注册**：`service/query/bootstrap.py` 按 `<PLATFORM>_ENABLED` 注册 DCIM（6 条）/ mgmt（28 条）/ security（占位）/ browser（页面截图，独立 Playwright 旁路容器）等平台 + skills 技能平台（无开关常驻，`skills/` 目录自动发现）。每平台 `capabilities.py`（能力声明）+ `endpoints.py`（EndpointSpec 接口）+ `client.py`（通用调用器）三件套；browser 平台用 `recipes.py` 声明式配方替代 endpoints。
- **promote 机制**：海量工具 schema 默认隐藏（`DeferredToolFilterMiddleware`），仅按意图预 promote 相关工具组；未命中时 LLM 用 `tool_search` 语义检索按需加载。
- **数据分析沙箱**：`analysis_load` 分页拉全量数据落盘（原始记录不进上下文）→ `analysis_exec` 在受限子进程跑 pandas（`python -I` + 内存/CPU 限制 + 禁网络/禁子进程 + 外部超时）→ `result` 变量产出 `chart_data`、文件产出 `artifacts`。
- **图表渲染**：LLM 输出 `<chart type="...">JSON</chart>`，前端 `ChartRenderer.tsx` 用 @ant-design/plots + @ant-design/graphs **本地渲染 26 种图表**，数据不出内网。
- **看板渲染**：用户要「看板/大屏」时，`skill.html_dashboard` 技能提供自包含 HTML 模板（数据内联 + ECharts），`analysis_write_file` 写入 → `analysis_exec(file=...)` 产出 `dashboard.html`，前端 `ArtifactPanel.tsx` + `HtmlPreview.tsx` 用 sandbox iframe + **打包内联 ECharts** 本地预览迷你看板，数据不出内网。
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

### 6. LLM 调度器链路

```
LLM 调用 → TaskScheduler.schedule/schedule_stream
  → 优先级 = TaskType（业务场景）× RoleLevel（角色权重）
  → 非流式无槽位入队 Redis 优先级队列等待；流式只抢槽不排队（超时 503）
  → 抢到槽位执行 → 释放并通知等待者
```

Redis 不可用 / 调度器禁用时降级为本地信号量。

## 预置权限

| 角色 | 权限 |
|------|------|
| `root` | 所有权限 |
| `rag_basic`（新用户默认） | `rag:chat` |
| `rag_admin` | `rag:chat/upload/delete/export` |
| `risk_user` | `risk:check/batch_check/report` |
| `risk_expert` | `risk:check/batch_check/fire_safety/report` |
| `query_user` | `query:platform/access`（Agent 平台工具访问） |
| — | `ai:agent`（Agent 对话 + 沙箱产物下载） |
| — | `ai:video`（视频生成） |

## 外部系统

| 系统 | 用途 |
|------|------|
| LLM（OpenAI 兼容） | 对话/视觉/嵌入统一经 `core/model_gateway.py` |
| Milvus | RAG 向量库（BM25 稀疏 + Dense 混合检索） |
| MongoDB | 用户/角色/申请/审计日志/视频任务/反馈 |
| Redis | 会话/令牌黑名单/缓存/调度器（prefix `rz:`） |
| PostgreSQL（可选） | Agent Checkpointer 持久化 |
| DCIM / 综合管理平台 / 安防平台 | Agent 平台工具后端（mgmt 支持认证中心按用户绑定账号） |
| MinerU | PDF 解析（`MINERU_ENABLED=true` 时启用） |
| Coze Studio | 后台 API 代理（已配置，`service/coze_proxy/` 未实现） |
