# 内网页面截图（browser 平台）

Agent 需要「看得懂页面」：进入登录后的内网系统（DCIM 动环 UI、mgmt `/center`），
按用户参数设置筛选，再截图并返回页面表格/正文。本能力与平台数据查询是两条路：
**截图用于「看一眼页面」；要精确数据请走对应平台的能力**。

## 架构

```
Agent(LLM)
  └─ 调用 browser__<recipe>(params)
       └─ service/query/browser/client.py            # 取配方、填参数、落盘截图
            └─ POST http://browser:8100/v1/capture    # 内网旁路容器，X-Browser-Token
                 ├─ Playwright: storageState → steps → wait ready
                 ├─ page.screenshot(full_page)
                 └─ DOM 抽取 text / table / html
            ← { image_b64, page{url,title}, text, tables }
       └─ 返回 <file url=... name=...> + 页面摘要
```

生产后端是 `uvicorn --workers 4`，有状态的浏览器会话不能放在后端进程里，所以执行器独立成
`browser` 容器；后端只持有「页面知识」（配方），容器只做通用执行。

| 位置 | 职责 |
|------|------|
| `service/query/browser/recipes.py` | 配方（页面路径 / 参数 / 步骤 / 就绪 / 抽取） |
| `service/query/browser/capabilities.py` | 配方 → Capability（`browser.<recipe_id>`） |
| `service/query/browser/client.py` | 调旁路服务、校验参数/主机、原子发布截图 |
| `service/query/browser/interactive.py` | 交互浏览 7 个工具（会话 id 派生、system 推断、截图/录屏落盘） |
| `service/query/browser/platform.py` | 注册到统一检索层（配方 caller 与交互 caller 分派） |
| `deploy/browser/app.py` | 旁路服务：`/health` `/v1/capture` `/v1/inspect` `/v1/session` |
| `deploy/scripts/browser_login.py` | 手动登录并保存 storageState（验证码/SSO 场景） |
| `deploy/scripts/browser_probe.py` | 探查页面结构，生成配方选择器 |

## 开关与部署

`BROWSER_ENABLED` **默认关**。关闭时：工具不注册、`<available-tools>` 不出现浏览器平台、
硬调会被 `DeferredToolFilterMiddleware` 拦。开启步骤：

1. 在 `deploy/config/.env.production` 配置：

   | 变量 | 说明 |
   |------|------|
   | `BROWSER_ENABLED` | 总开关（true 才注册能力） |
   | `BROWSER_SERVICE_URL` | 旁路服务地址，容器内默认 `http://browser:8100` |
   | `BROWSER_SERVICE_TOKEN` | 与旁路容器共享的 token（建议随机串，留空则旁路不校验） |
   | `BROWSER_TIMEOUT` | 单次捕获超时秒数（默认 60） |
   | `BROWSER_MAX_CONCURRENCY` | 旁路并发上限（默认 2，每实例约 1GB 内存） |
   | `BROWSER_ALLOWED_HOSTS` | 允许导航的主机白名单，逗号分隔 |
   | `BROWSER_MAX_IMAGE_MB` | 截图大小上限（默认 8；超出退回可视区截图） |
   | `BROWSER_STATE_DIR` | 登录态目录（容器内 `/state`） |
   | `BROWSER_DCIM_URL` / `BROWSER_MGMT_URL` | 页面根地址（不是 API 前缀） |
   | `BROWSER_DCIM_USERNAME` / `BROWSER_DCIM_PASSWORD` | DCIM 服务账号（登录配方用） |
   | `BROWSER_MGMT_USERNAME` / `BROWSER_MGMT_PASSWORD` | mgmt 服务账号 |
   | `BROWSER_EXPLORE_ENABLED` | 交互浏览开关（7 个工具，见下节） |
   | `BROWSER_MAX_SESSIONS` | 旁路容器同时存活的会话数（默认 4，超出按最近使用淘汰） |
   | `BROWSER_SESSION_IDLE_SECONDS` | 会话空闲回收秒数（默认 900） |
   | `BROWSER_RECORD_SECONDS` | 录屏默认时长秒数（默认 30） |
   | `BROWSER_MAX_RECORD_SECONDS` | 单次录屏上限秒数（默认 120） |
   | `BROWSER_RECORD_KEYFRAME_SECONDS` | 关键帧默认间隔秒数（默认 5） |
   | `BROWSER_MAX_KEYFRAMES` | 关键帧数量上限（默认 12，过密自动放大间隔） |
   | `BROWSER_MAX_VIDEO_MB` | webm 大小上限（默认 30，超限只交付关键帧） |
   | `BROWSER_RECORD_FPS` | 视频回放帧率（默认 10，上限 15） |
   | `BROWSER_CLICK_DENYLIST` | 点击黑名单，逗号分隔，**整体覆盖**内置激进词表；未设置用内置表，显式空串关闭拦截 |

2. 构建并启动旁路容器：

   ```bash
   cd deploy
   docker compose build browser
   docker compose up -d browser
   docker compose exec browser python test_app.py   # fixture 自测（截图/抽取/超时）
   ```

3. 确认健康：`docker compose exec backend curl -s http://browser:8100/health`。
   compose 已配 `shm_size: 1gb`、`mem_limit: 2g`、只连 `app-net`、不映射宿主端口。

4. 重启后端让开关生效（Agent 图按配置指纹缓存，`BROWSER_ENABLED` 已在指纹里）。

## 登录态

旁路容器按 `system` 读取 `/state/{system}.json`（Playwright storageState，**不含账号密码**）。
两种取得方式：

- **自动登录（推荐）**：配方里写 `login`，`client.py` 会把 `BROWSER_<SYSTEM>_USERNAME/PASSWORD`
  注入 `{username}`/`{password}`，登录成功后旁路服务回写 state。账号建议只授只读角色。
- **浏览器必须用专用只读账号**：mgmt 登录是单会话的 —— 同一账号在别处登录会作废旧 token
  （后端自己的 mgmt 数据查询也用同一账号，会互相踢）。共用账号时表现为「刚登录又被踢回
  登录页」，旁路服务会报 `stage=login_conflict`。
- **手动引导（验证码 / SSO / 扫码）**：在能访问内网的机器上执行

  ```bash
  pip install playwright && playwright install chromium
  python deploy/scripts/browser_login.py --system dcim --url http://<dcim-ui>/
  # 浏览器窗口里手动登录，回终端回车保存
  docker cp deploy/browser_state/dcim.json rz-browser:/state/dcim.json
  ```

## 配方校准（上线前必做）

仓库里的配方是**模板**（`enabled=False`，选择器写着 TODO）。未校准的配方不会注册能力，
避免「一调就错」暴露给模型。校准流程：

1. 探查真实页面（走已部署旁路容器，或本机 Playwright）：

   ```bash
   python deploy/scripts/browser_probe.py --system dcim \
     --url http://<dcim-ui>/alarm/list \
     --service-url http://browser:8100 --token "$BROWSER_SERVICE_TOKEN" \
     --steps-file /tmp/steps.json --ready-selector '#alarm-grid' --out /tmp/dcim.json
   ```

   `steps.json` 示例（需要先设筛选时）：

   ```json
   {"steps": [
     {"action": "fill", "selector": "#keyword", "value": "廊坊"},
     {"action": "select", "selector": "#level", "value": "重要"},
     {"action": "click", "selector": "#search"}
   ]}
   ```

   本机模式加 `--local`，登录态读 `deploy/browser_state/{system}.json`（需先跑
   `browser_login.py`；并要求本机装了项目依赖与 Playwright）。

2. 按 dump 结果把 `recipes.py` 里的 `steps` / `ready` / `extract` 选择器替换掉，
   把 `enabled` 置 `True`。选择器优先 `data-testid`、role+name、稳定 class；**禁用 nth-child**。

3. 重启后端 → 工具 `browser__<recipe_id>` 自动注册，`<available-tools>` 同步出现。

4. 对话里验证：「廊坊最近 24 小时重要及以上告警，截个图」→ 工具卡片 → 图片内联 →
   刷新会话后仍在。

## 交互模式（`BROWSER_EXPLORE_ENABLED`，默认关）

配方模式只覆盖「页面固定 + 筛选固定」。以下三类覆盖不到，需要模型自己看页面决定下一步：
列表里点进详情/弹窗、用户直接给一个内网链接、登录后还要切菜单才能到目标页。
交互模式就是这条闭环，**独立开关**，不开时这批工具既不注册也不进上下文。

| 工具 | 参数 | 行为 |
|------|------|------|
| `browser__navigate` | `url`（必填）、`system`（dcim/mgmt，可选） | 打开页面并返回可交互元素 `[ref]`；首次调用按 system 装载 storageState |
| `browser__snapshot` | 无 | 不操作，只重新编号当前页面元素 |
| `browser__click` | `ref`（必填） | 点最近一次 navigate/snapshot 返回的编号，返回新编号 |
| `browser__get_text` | `max_chars`（可选，≤8000） | 读当前页面可见文本 |
| `browser__screenshot` | `full_page`（可选） | 当前页面截图落会话产物，返回 `<file>` 用的 url/name |
| `browser__record` | `seconds`（5~120，默认 30）、`keyframe_interval`（默认 5）、`video`（默认 true） | 在当前页面录固定时长，返回关键帧 + 可选 webm（见下节「录屏」） |
| `browser__close` | 无 | 关闭本线程会话、释放 Chromium context |

**入口**：这些内网系统是单页应用（SPA），只保证**系统根地址**能打开；业务页要从根地址
按元素名字点进去（菜单 / 列表），**不要拼 `/pen/...` 这类接口路径** —— 那不是页面路由。
登录由平台按 system 自动完成，模型不需要也不接受用户凭据。

**识别口径（通用规则，不按站点/业务流写死）**：换一个新页面不需要改代码，模型按元素名字自己决定点哪个。

- 快照收录所有**可见**元素：`a / button / input / textarea / select / [role=*] / [onclick]`，
  外加「可见 + 有文字 + `cursor:pointer`」的元素 —— 内网 SPA 的顶部/侧边菜单常是无
  `role`、无 `onclick` 的 `div/li`，只认基础选择器会拿不到菜单。
- **遍历所有 frame**（含跨域 iframe）：综合管理平台把业务页放在 `oa.ddbes.com` 的 iframe 里，
  只扫主文档会得到空页面。`ref` 跨 frame 连续编号，`click` 时逐个 frame 查找。
- 表单控件名字前拼表单项标签（如「公司：请选择」「事件专业：请选择」），页面上多个
  「请选择」才区分得开；已填值优先于 placeholder。
- 快照/截图前等页面静默（`networkidle` + 加载遮罩淡出），避免截到空表格或转圈。
- 单次最多 200 个元素。纯图标按钮、canvas 绘图区、拖拽、虚拟滚动长列表不在识别范围内。
- **危险元素标记**：名字命中点击黑名单（删除/提交/保存/确认/审批/屏蔽/恢复…见下方「安全」）
  或本身是 `input[type=file]` 的元素，快照里带 `blocked: true`，`click` 会返回 `stage=blocked`
  且**不执行点击**。模型知道有该按钮但不能点，应告知用户需人工完成、不要反复重试。
  过滤类控件（`checkbox/radio/select`）不受影响，筛选照常可点。

**会话模型**：一个线程一个会话，会话 id 由后端从 `thread_id` 确定性派生（`sha1[:16]`），
**不进工具参数**。会话存放在旁路容器（单进程），因此 4 个后端 worker 都能命中同一会话，
不需要粘性路由。`navigate` 到另一个 system 时关闭旧 context 重建；超过
`BROWSER_MAX_SESSIONS` 按最近使用淘汰最久未用的；空闲 `BROWSER_SESSION_IDLE_SECONDS`
后被后台任务回收。超时**不关会话**（动作可能已生效）。

**成本**：每个会话约 1GB 内存，`BROWSER_MAX_SESSIONS` 要与 `mem_limit: 2g` 配套；
`BROWSER_MAX_CONCURRENCY` 仍限制全局并发。工具常驻可见（不进 deferred）——7 个工具
必须成套出现，缺一个流程就会在第二步断掉。

**安全**：`navigate` 的 URL 先过 `BROWSER_ALLOWED_HOSTS`，旁路服务用同一变量兜底；
协议只允许 http/https。截图沿用会话产物 ACL。**不提供文本输入工具（`fill`/`type`）**，
避免产生写副作用；需要输入关键字请用配方模式，下拉筛选可通过 `click` 完成。

## 录屏（`browser__record`）

在**当前已打开的页面**上录一段固定时长（5~120 秒，默认 30），随时可开始，**不重新加载页面、
不关闭会话**，录完还能继续 `snapshot` / `click`。两类交付：

| 交付 | 实现 | 说明 |
|------|------|------|
| **关键帧**（主交付） | 到点 `page.screenshot(type='jpeg')` | 按 `keyframe_interval` 采样，含 t=0 与末尾；总数超过 `BROWSER_MAX_KEYFRAMES` 时自动改成等距 |
| **webm 视频**（可选） | CDP `Page.startScreencast` 采帧 + ffmpeg 合成 | 默认开；`video=false` 时只出关键帧 |

**为什么不用 Playwright 原生录像**：`record_video_dir` 只能在 `new_context` 时指定，没有
start/stop，无法在用户进入监控页之后再开录。CDP screencast 可以对当前 page 随时启停。

**合成口径**：采集帧先落临时目录（按 `BROWSER_RECORD_FPS` 节流，默认 10fps），结束后按固定
帧率铺成时间轴（无新帧时保持上一帧，成片时长严格等于 `seconds`），用 ffmpeg `libvpx`(VP8)
编码，失败回退 `libvpx-vp9`。纯静态页收不到采集帧时，用最后一张关键帧合成静态视频。
`ffmpeg` 不在路径里、合成失败或成片超过 `BROWSER_MAX_VIDEO_MB` 时**只交付关键帧**并回
`video_error`，不判整次失败。临时目录在 `finally` 里清理。

**并发与超时**：同一会话在录制期间被 `session.lock` 独占（其他页面动作排队）；全局同时只允许
**一路录制**（独立信号量，第二次调用快速失败而不是排队），且录制不占全局捕获信号量，避免长录制
饿死其他截图。旁路服务超时 = `BROWSER_TIMEOUT + seconds + 90`，后端 = `BROWSER_TIMEOUT +
seconds + 120`。建议单次 30~60 秒：AGENT_EVENT_TIMEOUT(360s) 与 nginx `/api/` 读超时(300s)
仍会约束整轮时长。

**边界**：webm **无音频、无鼠标指针**；帧率上限 15fps（默认 10），画面细节以关键帧为准；
点击打开的新标签页不会被录到（录的仍是原 page）；Playwright Chromium 不含 H.264，若监控页
本身是 H.264 流可能整页黑屏，需先确认页面能播。**不生成 mp4**，也**不做自动视频理解**——
关键帧供用户查看，或留待后续接入视觉分析。

## 交付链路（复用现有产物机制）

- 截图写 `{AGENT_ANALYSIS_DATA_DIR}/{thread_id}/browser/<ascii>.png`；录屏的关键帧写
  `rec_frame_NN-<时间戳>.jpg`、视频写 `rec_video-<时间戳>.webm`。该目录无 `meta.json`，
  不会被数据集淘汰策略删除。
- **绝不覆盖**：`O_EXCL` 临时文件 + 硬链接发布 + inode 校验（对齐 DeerFlow）。
- 工具返回 `<file url name/>`；`api/v1/agent.py` 已把 png/jpg 映射为 `image/*`、webm 映射为
  `video/webm`。图片前端 `FileCard` 内联 `<img>`，webm 走鉴权端点 blob → `<video>`（`<video src>`
  带不上 Bearer），mp4 讲解视频仍走签名直链。
- 文件名纯 ASCII + 时间戳，避免中文名下载 404。

## 失败口径

旁路服务失败一律返回 `{ok:false, stage, error}`，`client` 转成可读提示：

| stage | 含义 |
|-------|------|
| `recipe` | 配方不合法（未知占位符/未知动作/参数含非法字符） |
| `login` | 登录配方执行失败（账号失效/验证码/SSO） |
| `login_page` | 目标地址不是登录页（URL 拼错，通常是拼了 `/pen/...` 这类接口路径） |
| `login_conflict` | 登录后又被踢回登录页（同一账号在别处登录），需为浏览器配置专用账号 |
| `navigate` | 打不开目标页（地址错/网络/证书） |
| `apply_filters` | 筛选步骤失败（选择器过期/元素不可用） |
| `ready` | 等待就绪超时（页面改版或接口慢） |
| `extract` | 抽取失败 |
| `screenshot` | 截图失败/Chromium 不可用 |
| `session` | 没有可用浏览会话（已关闭/过期），需先 navigate |
| `click` | 元素编号失效或点击失败，需重新 snapshot |
| `text` | 读取页面文本失败 |
| `blocked` | 目标是危险操作（删除/提交等），只读策略拒绝点击；页面状态未变 |
| `record` | 录制失败（页面未打开/录制期间操作页面/已有录制在进行中） |
| `video` | 视频合成失败（ffmpeg 不可用或未采集到帧），关键帧仍可用 |
| `timeout` | 整轮超时 |

页面改版会破坏配方，靠 probe 重新校准；**不静默**。

## 安全

- 导航目标由后端 `BROWSER_ALLOWED_HOSTS` 先行校验，旁路服务用同一环境变量兜底。
- 工具参数白名单 + 插值转义：只允许配方声明的参数，含换行/NUL 直接拒绝。
- 旁路服务只连内网 `app-net`、校验 `X-Browser-Token`，不映射宿主端口。
- 截图可能含敏感数据：沿用会话产物 ACL（`ai:agent` + 会话归属），与 PDF/看板一致。
- 服务账号建议只授只读角色，避免浏览器误操作产生写副作用。
- **点击黑名单**（只读浏览的第二道闸，账号只读权限是最后一道）：快照按元素名匹配词表，
  命中即标 `blocked: true`；`click` 时先读快照打的 `data-rz-blocked` 属性，SPA 重渲染丢了
  属性则按当前文本现场重算一次，命中就拒绝点击。内置词表是**激进表**（删除/移除/清除/重置/
  恢复/提交/保存/确认/编辑/修改/新增/导入/上传/导出/打印/撤回/撤销/取消/发布/审批/驳回/
  拒绝/通过/下发/控制/启停/停用/启用/禁用/注销/退出/屏蔽/抑制/解绑/绑定…），
  `BROWSER_CLICK_DENYLIST` **整体覆盖**它（不是追加）：未设置用内置表、设成空串关闭拦截。
  边界：按**可见文本**匹配，不是语义理解 —— 名字不含危险词的自定义按钮（如「执行」）、
  纯图标按钮拦不住；激进表必然误伤菜单名含「审批」「恢复」的页面（如「审批管理」），
  上线后用 `browser_probe.py` dump 真实页面，再按实际按钮名收窄或补词。

## 自测

```bash
# 后端配方层（纯 assert，不连网、不启浏览器）
python -m service.query.browser.tests.test_recipes
# 交互能力层（纯 assert：能力集/会话 id/导航校验/返回整形/录屏参数/blocked 透传与提示/开关守卫）
python -m service.query.browser.tests.test_interactive

# 旁路服务（需在容器内，依赖 Chromium）：navigate→click→screenshot→record→close 全流程，
# 以及 cursor:pointer 菜单识别/点击、iframe 内元素进快照且正文可读、录屏关键帧与 webm、
# 危险按钮在快照标 blocked 且点击被拒（页面状态不变）
docker compose -f deploy/docker-compose.yaml exec browser python test_app.py
```

## 已知边界

- **飞书群消息页面**：飞书 Web 群聊需真人登录，bot 凭据打不开。建议用
  `im.v1.message.list` 的数据渲染自包含 HTML，再让旁路服务 `set_content` 截图，
  不依赖飞书 UI。
- 超长页面：全页截图超过 `BROWSER_MAX_IMAGE_MB` 时自动退回可视区并标
  `image_truncated=true`。
- 内网自签证书：容器内 `ignore_https_errors=true`，仅用于内网系统。
- Chromium 单实例约 1GB 内存；`BROWSER_MAX_CONCURRENCY` 与 `mem_limit` 要配套。
