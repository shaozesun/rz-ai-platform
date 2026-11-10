"""个人微信 iLink（ClawBot）机器人通道。

镜像 `service/feishu/` 的结构：`client.py` 长轮询 + 选主，`channel.py` 报文解析，
`runner.py` 身份解析 + Agent 复用，`reply.py` 发送，`artifacts.py` 产物链接。
公共逻辑（绑定码 / 产物 token / 选主锁 / 标签改写 / SSE 消费）来自 `service/channels/`。

协议要点：HTTP/JSON 长轮询 `POST /ilink/bot/getupdates`（hold 35s），无 SDK、无公网
回调；**不能编辑已发消息、也无打字态**（无流式，累计到终稿一次发）；**不能冷启动主动推**
（须回带 `context_token`）；`get_updates_buf` 游标单消费 → 必须选主。
扫码接入（运维一次性动作，拿 bot_token）走命令行脚本 `deploy/scripts/wechat_login.py`。
"""
