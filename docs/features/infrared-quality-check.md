# 红外巡检图片质量检测（合规检查页 · 部门 + 勾选任务）

> 2026-11-06 上线。入口在 `/compliance`「不合规检查」页的卡片 → 整屏看板。
> 判定**不用 VLM**，调用外部 Gradio 服务 `INFRARED_QUALITY_API_URL` 的 `/predict_images`。

## 结论

**检测单位是「巡检任务」，不是全量自动扫。** 用户选部门 → 看到该部门的巡检任务列表 →
勾选要检的任务 → 开始检测。查过的任务标「已检测」（`checked_at` 非空），默认只勾未检测的——
每月只更新一部分任务，之前查过的不必重复查。整屏看板提供「全选未检测（N）」。

`status=1`（进行中）的任务没有检测时间与图片，**前后端都排除**（复选框禁用 + 服务端过滤）。

## 数据来源（mgmt 综合管理平台）

| 用途 | 接口 |
|---|---|
| 任务列表 | `GET /infrared/inspection/task/list/v1/{deptId}/{page}/{pageSize}` |
| 任务详情 | `GET /infrared/inspection/task/detail/v1/{taskId}` |
| 图片 | 详情里的 `picture`，S3 预签名 URL（7 天有效，无需鉴权头）；**检测时下载到本地留档** |

- 列表返回 `{count, pageCounts, pageSize, infraredInspectionTaskVOS:[…]}`，按 `pageCounts` 翻页拉全量。
- 详情 `inspectionDeviceTypes[].inspectionDevices[]` 跨类型展平成设备行；跳过 `delStatus` 非 0 的，
  **只保留有 `picture` 的设备**（其余是设备台账，没参与本次巡检，出行会把「未巡检」误算成「检测失败」）。
- 任务详情里的设备数可能远大于列表的 `deviceCount`（台账全量 vs 本次巡检台数），
  设备数一律取 `deviceCount`，另存 `photo_count`（有图片的设备数）。
- 列表结果缓存在 Redis `infrared_quality:tasks:{dept_id}`，TTL `INFRARED_QUALITY_TASK_CACHE_TTL`（600s）；
  看板「刷新任务」传 `refresh=true` 绕过缓存。
- 部门下拉复用 `service/doc_update/depts.dept_columns()`（与文档更新检查同一份固定部门清单）。

## 推理服务（Gradio 原始 REST，不引 `gradio_client`）

```
POST {base}/gradio_api/upload            multipart files → 服务器路径数组
POST {base}/gradio_api/call{api_name}    {"data": [[FileData…], 模型路径, "auto"]} → {event_id}
GET  {base}/gradio_api/call{api_name}/{event_id}   SSE
```

**Gradio 6 起文件入参必须是 FileData 对象**，传裸路径字符串会被 pydantic 拒
（`The 'meta' field must be explicitly provided in the input data and be equal to
{'_type': 'gradio.FileData'}`，2026-11-09 在 6.31.0 实测）：

```json
{"path": "/tmp/gradio/<hash>/a.jpg", "orig_name": "a.jpg",
 "meta": {"_type": "gradio.FileData"}, "_type": "gradio.FileData"}
```

`event: complete` 的 `data` 是三元组，`[2]` 为结果表（2026-11-10 改版实测）：

```
headers: ["文件名", "Prediction", "sharp", "borderline", "defocused", "错误"]
row:     ["298ROzNAi5U.jpg", "sharp", "97.9%", "2.1%", "0.1%", ""]
```

- 每张输入图片一行；`Prediction` 为 `sharp` / `borderline` / `defocused` / `uncertain`；
- 三个概率列是**带 `%` 的字符串**（未校准 softmax，仅作参考展示）；
- 失败时 Prediction 与概率留空、`错误` 列写原因。

解析按**表头名**定位列（大小写不敏感；无表头才回退旧位置 `0 / 1 / 3`），
「模型分数」取**预测类别那一列**的原值（如 `97.9%`），`uncertain` 或空值为空。
返回按**文件名**映射回本地路径；同名文件（不同目录）按**位置**回退；缺行则该设备无结论。

`preflight()` 用 `GET {base}/config`（5s）判断服务是否可达。

## 判定口径（fail-closed）

| 模型输出 | 判定 |
|---|---|
| `sharp`（`INFRARED_QUALITY_PASS_LABELS`） | 合格 |
| `borderline`（`INFRARED_QUALITY_REVIEW_LABELS`） | 待复核 |
| `defocused` | 不合格 |
| `uncertain` / 未知 / 空 | 检测失败（说明：模型输出未识别 / 无预测结果） |
| 下载失败 / 推理异常 | 检测失败 |

**认不出的标签一律「检测失败」，绝不默认合格。** 模型分数是未校准的 softmax 概率
（带 `%` 的字符串），仅作参考展示。

**只认新英文标签**（2026-11-10 接口改版）：旧中文输出（`清晰`/`临界状态`/`模糊或失焦`）
不在新标签集里，缓存命中的旧任务会判「检测失败」——勾「重新推理」重跑即可覆盖。

**没图片的设备不出行、也不算「检测失败」**：任务详情的设备是台账（`2EsTvRH1SPC` 列表
`deviceCount=36`、详情 553 台，只有 36 台有图），没图的那些只是没参与本次巡检。
整任务一张图都没有 → 任务级 `result_status='no_image'`、明细为空、计数全 0、照常写
`checked_at`（不重复查）。

## 失败降级

| 情形 | 处理 |
|---|---|
| 预检不可达 | 整轮 `failed`，每个勾选任务与 run 都写明确错误，不逐图空转 |
| 单个任务详情失败（重试 1 次） | 该任务 `failed`，**保留上次明细与 `checked_at`**，不中断其它任务 |
| 单图下载/推理失败 | 该行「检测失败」+ 说明，其余照常 |
| 连续 3 批推理连接失败 | 剩余标「检测服务中断」，该任务 `partial` |
| 任务没有任何有图片的设备 | 不出行、不判「检测失败」；任务 `result_status='no_image'` |

run 状态：全部任务失败 → `failed`；任一失败或部分中断 → `partial`；否则 `success`。

## 存储（MongoDB）

| 集合 | 粒度 | 内容 |
|---|---|---|
| `infrared_quality_tasks` | 一任务一条（`_id=task_id`） | 任务元信息 + `photo_count` + `checked_at/checked_by/result_status/error/elapsed/counts/rows[]`；重检整条覆盖 |
| `infrared_quality_runs` | 一次勾选检测一条 | `{run_id, dept_id, task_ids, created_by, created_at, status, counts, error, elapsed, refresh}`，只做批次索引 |
| `infrared_quality_predictions` | 一设备一条（`{task_id}:{device_id}`） | `{label, score, at}`，只存**模型原始标签** |

**图片证据落本地磁盘**：检测时本来就要下载图片送推理，顺手留在
`{INFRARED_QUALITY_IMAGE_DIR}/{task_id}/{device_id}{ext}`（按任务分目录、文件名做了路径穿越防护）。
明细读库时把 `图片链接` 改写成平台端点 `GET /scheduled-tasks/infrared-quality/image?task_id=&name=`，
老记录没有本地文件才回退原始预签名地址。行里存一个隐藏键 `_image`（不在 `columns.COLUMNS` 里，
前端不渲染、但检索能查到）记本地文件名。**不做清理**：预签名 7 天过期后本地这份是唯一证据。

**下载按「本地缺不缺图」决定，不是按「要不要推理」**：每轮检测先把本地已有的图直接复用，
`pending`（缓存未命中、要推理）与**预测缓存命中**的设备里凡是本地缺图的，一起下载一次。
所以给本地留档上线前查过的老任务**重跑一次检测就能补齐图片**，不必勾「重新推理」；补图失败
只记日志，不影响已缓存的判定。

每个任务文档自带结果行（几百行、100~200KB，远低于 16MB），所以「已检测」状态与明细同源。
**缓存必须存原始标签**：存「合格」这类判定词，下次重检会因不在 pass 标签集合里被判「检测失败」。
**写缓存只能用 `pymongo.UpdateOne`**：`bulk_write` 不认 mongosh 那种
`[{'update_one': {...}}]` 写法，会报 `is not a valid request` 且只留一条 warning——
2026-11-09 的日志显示缓存因此**从未真正写入过**（集合一直不存在），每次检测都在重复推理。

**历史文档里已存的「说明=无图片」行在读取时过滤、不删库**：`records` 端点直接丢弃这些行；
`list_checked` 用 Mongo 聚合（`$filter`/`$size`，只回计数不回 rows）按「有图片」的行重算四档
计数；任务列表的 `latest_run.counts` 也按重算后的任务计数求和。所以旧任务**不用重跑**，
页面上的计数与明细立刻变成只看有图片的设备；`photo_count` 对旧文档由同一聚合兜底推出来。

## 并发与租约

一次只跑一轮：`infrared_quality:sync` 租约（TTL 300s、30s 心跳、Lua token 比对释放）；
进程被 kill 后 ≤5 分钟自动过期。租约值里带进度 `{phase:'detail'|'predict', done, total, current}`，
面板 3s 轮询读取，租约消失即停止轮询。Redis 不可用时 fail-open（允许多跑，不锁死功能）。

## API（`api/v1/scheduled_tasks.py`，权限 `ai:agent`）

| 端点 | 说明 |
|---|---|
| `GET /scheduled-tasks/infrared-quality/tasks?dept_id=&refresh=` | `{enabled, departments, dept, tasks, running, progress, latest_run}`；`tasks` 不含 rows，带 `device_count/photo_count/counts/result_status` |
| `POST /scheduled-tasks/infrared-quality/check` | `{dept_id, task_ids, refresh?}` → `{started, task_ids}` 或 `{started:false, reason}` |
| `GET /scheduled-tasks/infrared-quality/records?run_id=` / `?task_id=` | 直接回 `{columns, records}`（不套 `{ok,data}`），给 `RecordsTable` 消费；`run_id` 合并各任务行并按 `(巡检任务, 序号)` 重排；有本地图时 `图片链接` 指向下面的 image 端点 |
| `GET /scheduled-tasks/infrared-quality/image?task_id=&name=` | 本地留存的巡检原图（`FileResponse`）；文件不存在回 404 |

## 配置（`config/settings.py`）

```
INFRARED_QUALITY_ENABLED=false              # 总开关；false 时面板不渲染（接口回 enabled=false）
INFRARED_QUALITY_API_URL=http://192.168.122.100:7860
INFRARED_QUALITY_API_NAME=/predict_images
INFRARED_QUALITY_MODEL=<服务器上的 best.pt 路径>
INFRARED_QUALITY_DEVICE=auto
INFRARED_QUALITY_TIMEOUT=300                # 单批推理（upload+SSE）
INFRARED_QUALITY_DOWNLOAD_TIMEOUT=30
INFRARED_QUALITY_DOWNLOAD_CONCURRENCY=4
INFRARED_QUALITY_BATCH=20                   # 每批上传/推理张数（分批只是传输方式，不丢数据）
INFRARED_QUALITY_PASS_LABELS=sharp
INFRARED_QUALITY_REVIEW_LABELS=borderline
INFRARED_QUALITY_DEPTS=                     # 空 = 复用 DOC_UPDATE_DEPTS
INFRARED_QUALITY_TASK_CACHE_TTL=600
INFRARED_QUALITY_IMAGE_DIR=data/infrared_quality  # 检测时留档的原图；容器内挂宿主目录
```

## 前端

- `web/src/components/infrared-quality-panel.tsx` — 合规页卡片：部门下拉、`N 个任务 · 待检测 M`、
  最近一次检测计数（合格/待复核/不合格/检测失败）、「刷新任务」；点击打开看板。
- `web/src/components/infrared-quality-board.tsx` — 整屏看板：两行顶栏（返回 / 标题）+ 部门 +
  `全部/未检测/已检测` 筛选 + 全选未检测 + 「检测选中（N）」+「重新推理」勾选 +
  「本次检测明细」（最近一次 run 的合并明细）。**任务列表与明细不同屏**：默认整屏任务表
  （每页 20 条，进行中不可勾，`no_image` 显示「无巡检图片」徽标），点任务行进入该任务明细，
  明细头「← 任务列表」返回（显示「N 台 · 有图片 M · 检测于 …」）。
- 明细表把 `图片链接` 列声明成图片列（`RecordsTable` 的 `imageColumns`）：缩略图点击看大图。
  平台端点要带 Bearer，`<img>` 带不了 → 组件内用 axios 拉 blob 再 `createObjectURL`
  （与 `FileCard.tsx` 同法）；外部预签名直链直接交给 `src`，失败给「打开链接」兜底。

## 自检

```bash
python -m service.infrared_quality.tests.test_infrared_quality   # 24 项，纯 assert、不连库
python -m service.infrared_quality.cli --dept A1 --company 廊坊 --task <taskId>  # dry-run
python -m service.infrared_quality.cli --predict /tmp/a.jpg       # 只推理并打印标签/分数
```

只读核对：`python -m service.infrared_quality.cli --dept A1 --unchecked`（列出未检测任务）。

## 手工验收

1. `.env` 设 `INFRARED_QUALITY_ENABLED=true` 并重启后端；
2. `/compliance` → 红外巡检图片质量卡片 → 选部门（如廊坊 · A1）；
3. 看板确认任务列表完整、进行中任务不可勾；
4. 勾若干任务 → 检测选中 → 观察进度、计数与明细表（明细只含有图片的设备）；
5. 再次打开：已检测任务显示计数与检测时间，默认不再勾选；无图片任务显示「无巡检图片」；
6. Gradio 未启动时应看到明确的不可达提示（整轮 failed），而不是空表。

## 已知边界

- Gradio 服务需在 `INFRARED_QUALITY_API_URL` 可访问（部署机网络内），不可达时功能可用但每轮都失败。
- S3 预签名链接 7 天过期；检测时本地留档后明细不再依赖它，但**修复前的老记录、或下载失败的图**
  仍只能显示原始链接（过期即打不开，页面提示「图片无法加载」）。
- 原图按任务累积、**当前没有保留期/配额**（每任务约「有图片设备数 × 单图体积」）。需要清理时
  按 `INFRARED_QUALITY_IMAGE_DIR/{task_id}` 整目录删即可（与 Mongo 文档无引用关系，删了只是回退原链接）。
- 部署挂载：`deploy/docker-compose.yaml` 里 `INFRARED_IMAGE_DIR`（宿主，默认
  `/opt/rz-ai/data/infrared_quality`）→ `/app/data/infrared_quality`；容器重建不丢图。
- **上传代码必须排除 `data/infrared_quality`**（rsync `--exclude '/data/infrared_quality'`）：
  本机开发图会覆盖生产证据图；带 `--delete` 时，本机没有的生产任务目录还会被整批删掉，
  且预签名地址 7 天过期、删了取不回。
