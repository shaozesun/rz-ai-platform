# 定时任务（定时报告/巡检）实现记录

> 日期：2026-08-26 | 决策：不搬 DeerFlow 的 scheduler/运维面板，用轻量方案（asyncio 轮询 + Redis 锁 + 复用现有 Agent 链路）

## 背景与决策

业务方确认有「到点自动跑 Agent」需求（定时生成巡检/数据报告）。研究过 DeerFlow 的定时任务与运维面板：

- DeerFlow 的 scheduler 带 lease/多实例/RunEventStore，为「多实例部署 + 长任务」设计，咱们单机部署用不上；
- DeerFlow 运维面板（Gateway Console API）本质是「运行事件存储 + token 聚合」，Langfuse 已覆盖同等价值（trace/token/cost），重复造轮子没必要。

结论：**不引入 DeerFlow**，用「asyncio 后台轮询 + Redis 分布式锁 + 复用 `AgentService().stream()`」实现，产物/审计/Langfuse 追踪自动复用。

## 关键设计

### 1. 跑一轮 = 调 AgentService().stream()

定时任务到点后调 `AgentService().stream(message=prompt, thread_id=rz-agent-sched-{task_id}[-{run_id}], ...)` 消费到 `done`，最终回复 + `<file>` 产物 URL 自动落库/进 Langfuse，无新 agent 入口。服务账号权限由 `SCHEDULED_TASK_SERVICE_PERMISSIONS` 配置（默认 `ai:agent,query:platform/access`）。

### 2. 调度器：asyncio 轮询 + Redis 锁

- `service/scheduled_task/scheduler.py`：后台循环每 `SCHEDULED_TASK_POLL_INTERVAL` 秒查 `enabled && next_run_at<=now` 的到期任务；
- 每个任务执行前抢 Redis 锁（`set rx:scheduled:{task_id}:lock <uuid> nx ex TTL`），只有抢到锁的 worker 推进 `next_run_at` 并真正执行；
- **锁内重读任务**（fresh）关闭「读旧快照后重复推进」竞态：等锁期间若已被别的 worker 处理（next_run_at 已未来），直接跳过；
- 多 worker（`WORKERS=4`）/多节点天然兼容（共享 Redis/MongoDB，锁全局）；节点崩溃恢复（lease/heartbeat）留待多节点高可用再补。

### 3. 调度类型

- `once`：指定时间跑一次，跑完自动禁用；
- `interval`：每 N 秒（`next_run_at = last + interval`）；
- `cron`：croniter 表达式（新增依赖 `croniter>=2.0`）。

### 4. 产物下载（任务级端点）

现有 `GET /api/v1/chat/agent/artifacts/{thread}/{dataset}/{file}` 剥 `rz-agent-` 校验 session 归属，定时任务线程无真实用户 session → 新增任务级端点：

`GET /api/v1/scheduled-tasks/{task_id}/runs/{run_id}/artifacts/{filename}`

从运行记录的 `artifact_urls` 反查产物文件路径（`{AGENT_ANALYSIS_DATA_DIR}/{thread}/{dataset}/{filename}`），拒绝路径穿越。

## 文件清单

**新增（后端）**
- `models/scheduled_task.py` — ScheduledTask / ScheduledTaskRun / ScheduleType / RunStatus
- `service/scheduled_task/__init__.py`
- `service/scheduled_task/repository.py` — MongoDB CRUD + `compute_next_run` + `resolve_artifact_path`
- `service/scheduled_task/runner.py` — 调 AgentService 跑一轮，捕获回复/产物，写运行记录
- `service/scheduled_task/scheduler.py` — asyncio 轮询 + Redis 锁
- `api/v1/scheduled_tasks.py` — CRUD + trigger + runs + 产物下载

**修改（后端）**
- `config/settings.py` — `SCHEDULED_TASKS_ENABLED` / `POLL_INTERVAL` / `LOCK_TTL` / `SERVICE_USER_ID` / `SERVICE_PERMISSIONS`
- `config/mongodb_conn.py` — init_indexes 追加 `scheduled_tasks` + `scheduled_task_runs`
- `main.py` — lifespan 启停定时任务调度器
- `api/v1/__init__.py` — 注册路由
- `models/user.py` — 权限 `scheduled_task:manage` + 管理员角色授予
- `requirements.txt` — `croniter>=2.0`

**新增（前端）**
- `web/src/api/scheduledTasks.ts` — API client
- `web/src/pages/ScheduledTasksPage.tsx` — 任务列表/新建/编辑/手动触发/运行历史/产物下载

**修改（前端）**
- `web/src/App.tsx` — 路由 `/scheduled-tasks`（权限 `scheduled_task:manage`）
- `web/src/components/platform-shell.tsx` — 侧边栏「AI 能力 → 定时任务」

## 验证

- 单元：`compute_next_run`（once/interval/cron）、SSE 解析、`<file>` 产物 URL 提取、thread_id 生成（已本地跑通）
- 前端：`npm run build`（tsc + vite）+ `npm run lint` 通过
- 运行验证（需 MongoDB/Redis/Agent 环境）：
  1. 建 `interval=60s` 测试任务 → 60s 后 `scheduled_task_runs` 出现 run，final_reply + 报告产物经任务级端点可下载
  2. 建 cron（时间设近一点）任务 → 到期触发；once → 触发后自动禁用
  3. `WORKERS=4` 下多 worker 观察只执行一次（Redis 锁生效）
  4. 定时任务 trace 出现在 Langfuse
  5. 失败任务 → run.status=failed + error 落库

## 后续（多节点高可用时）

- 锁上加 lease/heartbeat（TTL 续租 + 孤儿检测），节点崩溃后任务可被接管——v1 未做（用户确认保持简单）。
