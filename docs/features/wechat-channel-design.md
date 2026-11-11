# 微信 IM 通道接入设计（个人微信 iLink / ClawBot）

> 阶段 1：个人微信。阶段 2（企业微信智能机器人）另文补充。
> 目标：让用户在微信里直接和平台 Agent 对话，复用已有的 `AgentService().stream()` 链路。

## 一、为什么是 iLink（ClawBot）

腾讯 2026 开放的**个人微信机器人协议** iLink，HTTP/JSON + 长轮询，**无需 SDK、无需公网回调**：

- 服务地址：`https://ilinkai.weixin.qq.com`（`WECHAT_BASE_URL`）
- 凭据：扫码拿 `bot_token` + `ilink_bot_id`（`deploy/scripts/wechat_login.py`）
- 鉴权头：`Authorization: Bearer {bot_token}`、`AuthorizationType: ilink_bot_token`、
  `iLink-App-ClientVersion`、每次随机 `X-WECHAT-UIN`

## 二、协议要点（决定实现形态）

| 维度 | 事实 | 对实现的影响 |
|---|---|---|
| 流式 | 长轮询 `POST /ilink/bot/getupdates`（hold ≈35s），响应信封 `ret` / `msgs` / `get_updates_buf` | 后台任务，无需公网入口 |
| 编辑消息 | ❌ 不能编辑已发消息，**也无打字态接口** | **无流式**：累计到终稿一次发 |
| 主动推送 | ❌ 须回带入站 `context_token` | 不做定时主动推（协议不允许冷启动） |
| 游标 | `get_updates_buf` **单消费者** | 多 worker 必须选主（`rz:wechat:leader`） |
| 凭据有效期 | `errcode == -14` = token 过期 | 清凭据 + 停轮询，重新扫码 |
| 群消息 | iLink bot 为联系人形态，**仅私聊 p2p** | **不支持读微信群消息**（协议限制） |

游标持久化到 Redis（`rz:wechat:cursor`），比文件更耐容器重建。批次全部处理完才推进游标，
单条消息异常不拖垮整批。

**扫码接入（对齐 DeerFlow 实测）**：取二维码是 **GET** `/ilink/bot/get_bot_qrcode?bot_type=3`
（公开头、无鉴权）→ `{qrcode, qrcode_img_content}`；查状态是 **GET**
`/ilink/bot/get_qrcode_status?qrcode=…[&verify_code=…]` → `status`。请求带头
`iLink-App-ClientVersion`（把版本号打包成整数）+ `X-WECHAT-UIN`（base64 随机数）；
业务接口再加 `Authorization: Bearer {bot_token}` + `AuthorizationType: ilink_bot_token`。
`base_info.channel_version` 默认 **`1.0`**。

## 三、代码结构

```
service/wechat/
  client.py     httpx 单例 + 鉴权头 + LeaderLock 选主 + 长轮询循环 + 游标读写 + 凭据装载
  channel.py    报文解析（message_type==1 / from_user_id / item_list text）+ /connect 路由
  runner.py     身份解析（wechat_user_id）→ Agent → 累计终稿 → 一次性发送
  reply.py      sendmessage（纯文本，带 from_user_id=""）
  artifacts.py  产物下载 token + <file> 标签改写（复用 service/channels/）
  binding.py    一次性绑定码 + wechat_user_id 落库（复用 service/channels/connect_code.py）
  login.py      公开取码接口（GET get_bot_qrcode / get_qrcode_status），供命令行脚本扫码用
  state.py      凭据落盘 / 读取 / 清除（WECHAT_STATE_FILE，0600）
```

**扫码接入（运维一次性动作）**：跑 `python deploy/scripts/wechat_login.py`，用微信扫码
（二维码存 `wechat_qrcode.png`）→ 成功后凭据写入 `WECHAT_STATE_FILE`（`state.py`，0600）→
**重启后端**自动装载。这是把微信号授权成机器人的动作（拿 `bot_token`），只有运维做一次，
**不给普通用户**（谁扫到即拿到机器人控制权）。`errcode -14` 时清凭据、停轮询，重跑脚本即可。

### 与飞书共享的公共件（`service/channels/`）

为「个人微信 + 企业微信」两通道确实要复用的部分抽出，飞书同步切过去（零回归）：

- `connect_code.py` — 一次性绑定码（各通道传 `prefix` / `ttl`）
- `artifact_token.py` — 签名下载 token（`prefix` 隔离命名空间）
- `leader.py` — `LeaderLock(key, ttl)` 多 worker 选主
- `file_tags.py` — `<file>`/`<chart>`/`<mermaid>` 标签降级（各通道传产物端点前缀）
- `agent_stream.py` — `parse_sse` + `iter_agent_events` 消费 SSE（飞书刷卡片 / 微信缓冲终稿共用）

**刻意不抽**：各通道事件/报文解析（WS 事件、长轮询报文、SDK frame 各不相同）、发送实现。

## 四、配置与集成点

- `config/settings.py`：`WECHAT_ENABLED`(默认 False)、`WECHAT_BOT_TOKEN`、`WECHAT_ILINK_BOT_ID`、
  `WECHAT_BASE_URL`、`WECHAT_CHANNEL_VERSION`(默认 `1.0`)、`WECHAT_ALLOWED_USERS`、`WECHAT_POLL_TIMEOUT`、
  `WECHAT_RETRY_DELAY`、`WECHAT_CONNECT_CODE_TTL`、`WECHAT_ARTIFACT_TOKEN_TTL`、`WECHAT_LEADER_TTL`、
  `WECHAT_STATE_FILE`(默认 `data/wechat/auth.json`)、`WECHAT_QR_BOT_TYPE`、`WECHAT_QR_TIMEOUT`、`WECHAT_QR_POLL_INTERVAL`
- `main.py`：`WECHAT_ENABLED` 时 lifespan 启停轮询任务（无凭据则记「未接入」，等脚本扫码后重启装载）
- `models/user.py`：`wechat_user_id` / `wechat_name` / `UserPublic.wechat_bound` / `wechat_name`
- `api/v1/wechat.py`：用户侧 `GET /status`、`POST /connect`、`POST /unbind`、`GET /artifacts/{token}`
- `middleware/auth_middleware.py`：放行 `/api/v1/wechat/artifacts`
- 前端：`web/src/api/wechat.ts` + `AuthCenterPage.tsx`（微信绑定区）
- 部署：`deploy/docker-compose.yaml` 命名卷 `backend_wechat:/app/data/wechat`（凭据持久化）

## 五、部署与验证

1. `.env` 设 `WECHAT_ENABLED=true`，重启后端：无凭据时日志见「未接入：缺少 bot_token」
2. 跑 `python deploy/scripts/wechat_login.py` → 出现 `wechat_qrcode.png` → 手机扫码
   （若提示校验码则输入）→ 凭据写入 `WECHAT_STATE_FILE` → **重启后端**，日志见通道启动
3. 平台「认证中心」生成绑定码 → 微信私聊发给机器人 → 回「绑定成功」
4. 微信提问 → 收到最终回复（无流式）；重启后端自动读 `WECHAT_STATE_FILE` 轮询，不重复消费旧消息（游标）
5. Agent 产出文件 → 回复含 `/api/v1/wechat/artifacts/{token}` 下载链接
6. 模拟 `errcode=-14` → 清凭据 + 停止轮询，重跑脚本重新接入

扫码接入（唯一入口）：`python deploy/scripts/wechat_login.py` 扫码（二维码存 `wechat_qrcode.png`），
凭据自动写入 `WECHAT_STATE_FILE`，重启后端生效（也打印 `.env` 两行备用）。

## 六、边界（本次不做）

- 个人微信媒体收发（图片/文件/语音，CDN AES 链路）
- 微信群聊读取（协议不支持）
- 定时任务主动推个人微信（协议不允许冷启动）
- 企微智能机器人（阶段 2）
