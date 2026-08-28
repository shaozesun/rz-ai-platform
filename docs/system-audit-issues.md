# 系统全面排查问题记录（2026-08-27）

> 范围：功能（后端/前端）+ 系统层面（安全/配置/性能/部署）。
> 状态统一「未处理」，修复后在对应项改「已修复」并补提交号。

## 🚨 高危（安全）

1. **密码重置账号接管** — `api/v1/auth.py` reset-password 在 PUBLIC_PATHS、仅图形验证码（三位数±1 很好算）+ 手机号即可重置任意账号密码，**无短信验证**。影响：任意手机号被接管。状态：未处理
2. **生产 Milvus 暴露无鉴权** — `deploy/docker-compose.yaml` 暴露 19530，`deploy/config/.env.production:87` `MILVUS_TOKEN` 为空。影响：向量库可被任意读写。状态：未处理
3. **WireGuard 私钥打进 Docker 镜像** — `deploy/Dockerfile:19` `COPY . /app`，`.dockerignore` 未排除 `deploy/vpn`（含真实 PrivateKey）。影响：镜像泄露即可接入内网。状态：未处理
4. **生产多弱口令/明文密钥** — `deploy/.env`、`deploy/config/.env.production`：Grafana `admin`、`MONGO_PASSWORD`、`MGMT_PASSWORD=666666`、MinIO `minioadmin/minioadmin` 等全明文落盘。状态：未处理
5. **pen 人员 token 缓存越权** — `service/query/mgmt/http.py:365` `get_person_token` 缓存 key 只含 `kingdee_uid` 不含 `auth_user_id`，B 用户会命中 A 换发的 token，绕过 A 的授权鉴权。状态：未处理
6. **当前 `.env` 为 `SMS_DEV_MODE=true`** — `_validate_captcha` 跳过全部验证码（登录/注册/重置）。`.env:11`。状态：未处理

## ⚠️ 中危（稳定性/并发/安全）

- **RBAC 角色权限缓存跨 worker 不同步** — `core/rbac.py:10` 进程内 dict，`invalidate_role_cache` 只清当前进程；多 worker（WORKERS=4）下角色/权限变更不生效，需重启。状态：未处理
- **JWT_SECRET 随机生成/硬编码** — `core/auth_engine.py:8` 未配置时 `secrets.token_hex(32)`，多 worker/重启 token 全失效；现 `.env` 硬编码。状态：未处理
- **事件流超时后 `astream_events` 未 aclose** — `service/agent/streaming.py:186` 超时抛 TimeoutError 后生成器未关闭，后台 LangGraph 任务泄漏，反复超时累积。状态：未处理
- **注册竞态：手机号无唯一索引** — `service/auth/user_service.py:117` `get_by_phone` 后 insert 非原子，并发同号注册出重复 PENDING 用户。状态：未处理
- **本地信号量降级逻辑失效** — `core/scheduler/semaphore.py:37` `acquire()` 恒 True、`try_acquire()` 不递减，Redis 故障降级时并发不受限；无 lease/TTL 槽位泄漏。状态：未处理
- **重排序并发无上限** — `core/model_gateway.py:341` `asyncio.gather` 无信号量，`RAG_CROSS_ENCODER_PRE_FILTER=50` 时 50 路并发 LLM，易打爆模型服务。状态：未处理
- **mgmt HTTP 401 绕过 token 刷新重试** — `service/query/mgmt/http.py:447,550` `raise_for_status()` 在 body code 判断前，真实 HTTP 401 直接抛 httpx 异常，进不了 `_is_auth_error` 重登分支。状态：未处理
- **每次调用新建 httpx.AsyncClient 无连接复用** — `service/query/mgmt/http.py` 多处 `async with httpx.AsyncClient`，高并发下连接开销大。状态：未处理

## 🖥 前端功能

- **空气泡无错误提示（高）** — `src/stores/chatStore.ts:81-84、426-429` onError 仅 console + 重置 isStreaming，气泡空白无提示；`showThinking` 停止后既无思考态也无错误文案。状态：未处理
- **会话切换竞态（高）** — `src/stores/chatStore.ts:284-298` `selectSession` 无请求取消/序列号，快速切 A→B 时 A 的 getMessages 晚到覆盖 B。状态：未处理
- **SSE 尾部截断（中）** — `src/api/chat.ts:99-124、210-247` 循环结束剩余 buffer 未 flush、`TextDecoder` 未最终 decode，末行无换行丢内容（可能与「回复截断」相关）。状态：未处理
- **自动滚动无上翻检测（中）** — `src/components/ChatArea.tsx:504` 每次 messages 变化强制 scrollTop=scrollHeight，流式中用户上翻会被拉回底部。状态：未处理
- **videoStore.loadTasks 无 catch（中）** — `src/stores/videoStore.ts:23-31` 只有 finally 无 catch，网络失败 unhandled rejection。状态：未处理
- **缓存命中未恢复 agentEnabled（中）** — `src/stores/authStore.ts:66-73` 缓存路径未设 agentEnabled，getMe 失败时恒 false，`sendMessage` 的 isAgent 走错分支。状态：未处理
- **sendMessage 与 newSession 竞态（低）** — `src/stores/chatStore.ts:371-383` createSession await 期间点新建清空消息，后续 appendChunk 无气泡可写，chunk 静默丢失。状态：未处理

## 🐍 后端功能

- **空回复无提示** — `service/agent/streaming.py:278-448` 模型返回空 content 且无异常时只发 done，用户看到空气泡。状态：未处理
- **mgmt department→deptId 映射缺失** — `service/query/mgmt/client.py:102-105` `header_dept_id` 读 `department` 但不在任何 param_map，注释声称映射实际不会。状态：未处理
- **mgmt_login phone 分支死代码** — `service/auth/auth_service.py:114` 两分支相同，非手机号用户名被当手机号。状态：未处理

## ⚙️ 系统/配置/性能

- **CORS 生产为 `*`** — `deploy/config/.env.production:124`、`config/settings.py:217`，未限制可信来源。状态：未处理
- **健康检查覆盖面不足** — `api/v1/health.py:9-35` 仅查 MongoDB/Redis，未覆盖 Milvus/LLM/MinerU。状态：未处理
- **大响应无分页** — `api/v1/rag.py:198,273`、`api/v1/video.py:99` `to_list(None)` 全量加载。状态：未处理
- **日志泄漏** — `.env` LOG_LEVEL=DEBUG、`core/sms.py:20` dev 模式验证码明文写日志；`.dockerignore` 未排除 `logs/`。状态：未处理
- **`.gitignore` 引用的 `.env.example` 不存在** — 无配置模板参考；`.env`/`deploy/.env` 未纳入版本控制，无变更追踪。状态：未处理
