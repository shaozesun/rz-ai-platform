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

润泽 AI 平台 — 统一 ToB AI 服务，由 risk-api（安全隐患检测）和 rz-rag（RAG 知识库）合并而成。认证体系为手机号+短信验证码+RBAC 权限模型。

### 分层

```
api/v1/     →  service/    →  core/       →  config/
  (路由)        (业务逻辑)      (基础设施)      (配置/连接)
                  ↓
               models/ (Pydantic DTO)
```

- **api/v1/** — FastAPI Router：`auth.py`, `admin.py`, `rag.py`, `chat.py`, `risk.py`, `video.py`, `stats.py`, `feedback.py`, `health.py`
- **service/** — 业务逻辑层：`rag/`, `risk/`, `auth/`, `video/`, `query/`, `coze_proxy/`
- **core/** — 基础设施：`model_gateway.py` (LLM 统一入口), `auth_engine.py` (JWT), `rbac.py` (权限), `sms.py`
- **config/** — 配置与连接：`settings.py` (pydantic-settings, 从 `.env` 加载), `mongodb_conn.py`, `redis_conn.py`, `logger_config.py`, `trace_id.py`
- **models/** — Pydantic 数据模型：`user.py` (User/Role/Permission + 预置角色), `risk/schemas.py`, `rag/schemas.py`, `video.py`
- **middleware/** — ASGI 中间件：`auth_middleware.py` (JWT 注入 user_id/permissions 到 `request.state`)
- **repository/** — RAG 数据层：`vector_store.py` (Milvus), `loader.py`, `splitter.py`, `sparse_embedder.py`
- **web/** — React + TypeScript + Vite + Ant Design 前端
- **prompts/** — LLM 提示词模板：`rag/`, `risk/`, `query/`
- **data/** — BM25 稀疏向量词表 (`sparse_vocab_group_default.pkl`)
- **embedded/** — 视频生成产物输出目录
- **deploy/** — Docker Compose 生产部署配置 + 备份恢复脚本

### 认证鉴权

- 手机号 + 短信验证码登录，开发模式 `SMS_DEV_MODE=true` 跳过真实发送
- 开发模式 `PERMISSION_OPEN_MODE=true` 时认证用户自动获得全部权限（跳过 RBAC 检查）
- JWT access_token (30min) + refresh_token (7d)，RSA 密钥对签发
- RBAC: User → Role → Permission (`resource:action`)
- 新用户默认 `rag_basic` 角色 (仅 `rag:chat`)，其他功能需申请 → Root 审批
- `@require_permission("risk:check")` 装饰器保护端点，中间件检查登录态后从 MongoDB 加载权限

### 核心模块

- `core/model_gateway.py` — 统一 LLM 网关 (单例)，方法：`chat()`, `chat_stream()`, `vision()`, `embedding()`, `batch_score()`。视觉和对话客户端可指向不同 API 端点
- `core/auth_engine.py` — RSA JWT 签发/验证/刷新 + Redis 黑名单
- `core/rbac.py` — `load_role_permissions()` 从 MongoDB 加载 + 内存缓存，`@require_permission` 装饰器
- `middleware/auth_middleware.py` — ASGI 中间件，提取 Bearer token → 验证 → 注入 `request.state.user_id/permissions`
- `repository/vector_store.py` — Milvus 向量库管理，BM25 稀疏 + Dense 混合检索

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
| `query_user` | `query:platform/access` |
| — | `ai:video` (视频生成) |

### 外部系统

- Coze Studio — 独立 Go 服务，后台 API 代理调用 (配置了但 `service/coze_proxy/` 未实现)
- LLM — OpenAI 兼容 API，视觉模型默认 `qwen3.5-vl`，对话模型默认 `qwen3.5`
- Milvus — 向量数据库，default: `http://localhost:19530`
- MongoDB — `rz_ai_platform` database，存储 users/roles/applications/audit_logs/video_tasks/feedbacks
- Redis — Session/Token 黑名单/缓存，prefix `rz:`
- MinerU — PDF 解析服务 (mineru.net API)，`MINERU_ENABLED=true` 时启用

### 已知未完成模块

- `service/query/` — 仅空 `__init__.py`，权限 `query:platform/access` 已定义但无实现
- `service/coze_proxy/` — 仅空 `__init__.py`，Coze Studio 代理未实现

### 前端

React + TypeScript + Vite + Tailwind CSS 4 + shadcn/ui。入口 `ChatPage.tsx`，`platform-shell.tsx` 侧边栏展示功能入口（知识库/隐患检测/消防配置/视频生成/权限中心）。

**页面：** `LoginPage`, `ChatPage` (RAG 对话), `KnowledgePage` (知识库管理), `RiskPage` (隐患检测), `FireSafetyPage` (消防配置), `VideoPage` (视频生成), `PermissionPage` (权限申请/审批), `OverviewPage` (仪表盘), `InnovationPage` (创新工坊), `SettingsPage` (设置), `admin/` (管理后台)

**状态管理：** Zustand — `authStore`, `chatStore`, `appStore`, `videoStore`

**API：** Axios client (`web/src/api/client.ts`)，按模块拆分 `auth.ts`, `chat.ts`, `rag.ts`, `risk.ts`, `video.ts`, `admin.ts`, `stats.ts`
