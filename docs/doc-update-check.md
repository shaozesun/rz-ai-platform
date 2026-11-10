# 月度文档更新检查（各公司 A 部门 × 25 项）

> 2026-10-30 上线（单树 sha256 快照）；2026-11-03 改为**逐部门共享文档**检查；
> 2026-11-04 部门清单改为**固定配置 + 缓存**；合规页卡恢复为 25 项列表（部门/期间筛选），
> 2026-11-04 整屏看板同步改为同一份列表（矩阵不再使用）；2026-11-05 扩为**多公司**（各园区 A 部门）。
> 清单来源：`~/Downloads/平台每月所需更新文件目录（修订版）.pdf`（1 页 36 条），只做 **25 条月度项**。

## 结论

**每家公司的每个 A 部门**各有一份共享文档目录，月报等文件是各部门**各自上传**的，内容互不相同。
检查口径是 **25 条清单项 × 各公司各部门**（5 家国内园区共 21 个部门 × 25 项 = 525 格），页面以
**6 列列表 + 部门/期间筛选**展示，一次只看一个部门（25 行）。合规页卡与整屏看板是同一份列表，
筛选状态共享。

**同名部门（A1、A2…）在不同公司是不同目录**，所以 `company_id` 是快照、判定、筛选项、行 key
都必须带的维度。页面下拉显示「公司 · 部门」（如「平湖 · A1」），多于一家公司时标题写
「N 家公司 · M 个部门 × 25 项」。

**部门清单是固定配置，不随每次检查动态发现**：`DOC_UPDATE_DEPTS` 按公司分组写死
（廊坊 A1、A2、A3、A5~A12、A18；平湖 A1/A2；惠州 A2/A3；佛山 A1/A2/A3；重庆 A1/A2）。
接口只用来把部门名解析成 `dept_id` / 共享根 `root_fid`，结果落 Mongo `doc_update_depts` 缓存。
因此 **部门下拉不等快照**——一期都没跑过也能列出全部部门（列表显示 `—` / 「未找到」）。
新增/撤掉公司或部门改这一行配置，再跑一次 `--refresh-depts`。

**更新判据按目录形态自动分两种**（不是手写 25 项分类）：

| 形态 | 判定 | 依据 |
|---|---|---|
| 按期提交型 `period` | 看本期应到月份（或更晚）里**有没有文件** | 路径里有月份目录（`2026/9月`、`2026-09`、`2026年9月`）；**不下载、不哈希、不需要上期快照** |
| 原地更新型 `flat` | 下载最新文件算 sha256，与**上一期快照**比 | 没有月份目录（整定表/阈值表这类一份文件长期修改） |

形态由 `period.detect_layout()` **从目录结构自动判定**，判错也只影响判据，不吃数据。
能源月报、各类月度记录表、排班/考勤这类每月新起一份的都属于按期提交型。

## 为什么不用 `updateTime` / `uploadTime` / `version`

只读探针 `deploy/scripts/doc_update_probe.py` 实测：

| 字段 | 实测表现 |
|---|---|
| `updateTime` / `uploadTime` | 方向不一致、大批文件同一天 `uploadTime`，无法据此判断「本期是否更新」 |
| `FileVO.version` | 是**文档标题**，不是版本号 |
| 文件大小 `size` | 与真实下载字节偶尔不符（见「待复核」） |

原地更新型只信内容哈希：同 fid 两次下载 sha256 一致。按期提交型只看目录结构，连字节都不用取。

## 取数链路（全部串行）

mgmt 同账号 × 同接口**同时只允许 1 个在途**，故整条链路串行。

| 步骤 | 接口 | 用途 |
|---|---|---|
| 部门清单 | `GET /dept/dept/list?companyId=…`（**逐公司一次**） | **只解析 `dept_id`**（列清单来自 `DOC_UPDATE_DEPTS` 固定配置）；结果按 `(company_id, dept_name)` 落 `doc_update_depts` 缓存，缓存齐了不再调用 |
| 部门根目录 | `GET /dept/dept/details?deptId=` | `userList[0].rootFileId`（该部门共享文件夹根） |
| 共享目录 | `GET /file/share/v2/{parentId}/2/{page}/{pageSize}` | `data.shareFileVOList[]`：`{fid,name,type,parentId,createTime,size}`，每页上限 100 |
| 共享下载 | `GET /file/share/v1/minio/url/{fid}` | 原地更新型取预签名地址，`httpx.stream` 边下边算 sha256 |

**共享接口两条实测硬约束**：

- `type == '1'` 是文件夹，**其余一律当文件** —— 共享文件节点的 `type` 常是空串 `''`，不是整树接口里的 `'0'`；
- `shareFileVOList` 可能为 `null` → 当空目录。

**编号剪枝（fail-open）**：文件夹名前导数字串与**全部目标编号**都明显不兼容时才跳过
（目标是 `1.4.5`、目录是 `2.x`）；`1.4.5` 与 `1.4.5.1` 视为兼容，无数字名字一律下钻。
**目标编号 = 清单编号 ∪ 实际目录名里的编号**（如 `8.1.2.3`），否则会在祖先层被剪掉。
宁可多走几页，不可漏匹配。单目录失败只记 warning，不拖垮整部门。

预签名地址是对象存储域名，**不带** `token`/`Deptid` 头（避免把平台 token 发给 S3）。

## 期号解析（只看目录名）

`period.parse_period(segments, fallback_year)` 从路径的目录段解析月份级期号 `'YYYY-MM'`：

- 支持 `2026-09` / `2026/09` / `2026_9` / `2026.9` / `2026年9月` / `9月`；
- 目录名带业务标题时月份夹在中间也认：`润泽国际信息港A-1能源报告【2025年10月总第116期】`
  → `2025-10`（只认 `YYYY年M月` 这种无歧义写法，`2026-11更新` 不模糊匹配）；
- 月份段向上找最近的四位年份段补年份，找不到就用该文件 `createTime` 的年份；
- **只有年份或没有月份 → 解析不到**（`2026`、`2026年` 不算月份级），`1999-09` 这类越界年份不认；
- **绝不用文件名**：文件名里的日期是坑（如 `…2026-11更新.xlsx`），会让原地更新型每月都被当成新一期。

**本期应到** = 当前月回退 `DOC_UPDATE_PERIOD_GRACE_MONTHS`（默认 1，即上月）。
这样两种命名习惯都能过：按报告期（10 月报叫 `2026-09`）和按交付月（叫 `2026-10`）。

## 判定口径

| 状态 | 判据 |
|---|---|
| 已更新 `updated` | 按期提交型：应到期或更晚的期号里有文件；原地更新型：sha256 与上期不同（新增文件也算） |
| 未更新 `stale` | 按期提交型：最新一期早于应到期；原地更新型：sha256 相同 |
| 未找到 `missing` | 该部门下未查询到相关文件 |
| 下载失败 `failed` | 取不到字节 / 内容类型校验不过 —— **不是「未更新」** |
| 基线 `baseline` | 原地更新型首期无上期快照，只建基线 |

- **按期提交型永远不会是基线**：不依赖上期快照，首轮就能出真实结论。
- **逐部门基线**：上期快照里没有该部门（新增公司/部门）时，它的原地更新型记 `baseline`，
  不误报「已更新」；按期提交型仍直接判定。
- 部门根目录取不到 → 该部门所有格记 `failed`，不中断其它部门。
- `verdict.baseline` 只描述「原地更新型有没有上期可参照」；页面上按每格状态取即可。

匹配规则：**实际名称优先，编号兜底**。PDF 里的清单名只是模糊叫法（「能源月报」），
真实共享树里的写法不一样 —— 同一份月报在 A1 下就存了三处：
`1.4.5能源管理报告（能源月报同步）`、`1.2.3容量管理报告（能源月报同步）`、
`8.1.2.3能源管理月报`（只登记编号 `1.4.5` 会漏掉另两处）。故 `catalog.DocItem.folders`
登记实测到的真实目录/文件名，路径任一段或文件名**包含**它即命中（不再过关键词闸）。

编号仍作兜底，可以落在**文件名**上（`4.1.5供配电隐患排查表.pdf`），也可以落在
**目录名**上（部门共享树通常如此，文件名是业务标题、不带编号）。
前缀后面**不能再接数字或点**（防 `1.6.6.1` 命中 `1.6.6.10`）；`4.1.5` 四条同名不同表
靠名称关键词消歧（只对编号命中生效）；`1.6.6.1-1.6.6.3` 展开成三个前缀。

命中会记下 `match_dir`（最深命中的那一级目录名），快照项再聚合成 `actual_dirs`，
页面明细显示「实际目录」—— 便于核对到底命中了哪个文件夹。发现新的实际写法时，
只改 `catalog.py` 的 `folders`，匹配逻辑不动。

「待复核」（`warn`）：档案 `fileType` 与实际字节不符，或实际字节与档案 `size` 差 > 10%。只提示，不当失败。

## 快照结构

Mongo `doc_update_snapshots`，同 `period_key`（`YYYY-MM`）覆盖，一期一条：

```jsonc
{
  "period_key": "2026-11",
  "period_label": "2026年11月",
  "scope": "dept",
  "depts": [
    {"dept_id": "...", "dept_name": "A1", "root_fid": "...", "error": "", "items": [
      {"seq": 2, "number": "1.4.5", "name": "能源月报",
       "layout": "period",              // period = 按期提交；flat = 原地更新
       "expected_period": "2026-10",
       "latest_period": "2026-09",
       "actual_dirs": ["1.4.5能源管理报告（能源月报同步）"],
       "files": [{"fid": "...", "file_name": "...", "path": "...",
                  "match_dir": "1.4.5能源管理报告（能源月报同步）", "period": "2026-09",
                  "sha256": "", "error": "", "warn": "", "last_changed_at": ""}]}
    ]}
  ]
}
```

`verdict.evaluate()` 按「快照里有没有 `depts`」分派：有 → 部门分支（产出扁平 `items` 带
`dept_id`/`dept_name`/`layout`/`latest_period` + `depts` 汇总）；无 → 旧单树分支（旧快照照常可读）。

## 重复检查（同月再查）

**只有两种口径**，由 `evaluate()` 返回的 `mode` 决定：

| mode | 参照物 | 展示词 |
|---|---|---|
| `cross` | **上一期**快照（不同 `period_key`，即上月） | 已更新 / 未更新 —— 唯一判定口径 |
| `baseline` | 没有上期 | 原地更新型记基线；按期提交型仍直接判定 |

**是否更新只与上月比，不与「上一次执行」比。** 这条靠存储结构保证：

- `store.previous(period_key)` 只取**严格早于该期**的最近一期
  （`period_key <` + 按 `period_key` 倒序，不按 `taken_at`）；
- 同月重跑 `store.save()` 只 `$set` **本期**那份（`snapshot_id`/`created_at` 保留），
  上月那份永不被写 → 本月某文件一旦与上月不同，「已更新」整月不变，反复查也不会翻；
- 没有上期就是基线期：**不拿本月早先的检查结果当参照**。

页面面板、看板、`GET /doc-update/overview` 都走 `build_overview()`，对话工具 `doc_update_check` 同理，三处口径一致。

## 历史期查看（2026-11-04）

- 页面「期间」下拉列出 `doc_update_snapshots` 里的**所有期**（`store.list_periods()`，
  新到旧，一月一条、不做条数上限），默认「最新」。
- `GET /doc-update/overview?period=YYYY-MM` 只读指定期；期号不合法 → 400；
  库里没有该期 → 回 `missing_period`，页面提示「该期没有快照」。
- 历史期的参照仍是**严格上一期**（见上节）：补跑旧期不会把后写入的旧期当成参照；
  正常按月递增时结论与旧口径一致。
- `evaluate(..., now=snapshot['taken_at'])`：历史期的「上次变更 N 天前」按
  **该期采集时刻**算，不是按今天。
- 旧单树快照照常可读（25 项列表），但没有部门维度 → 只能看到全部 25 项。
  判据是 `verdict.scope === 'dept'`，**不看** `dept_columns` 是否有值
  （部门列是常驻配置，旧单树历史期也会带着它）。
- 「部门」下拉**默认第一个部门**（25 行），一次只看一个部门；下拉里不再有「全部部门」。
  合规页卡与整屏看板共用同一个筛选状态。
  「立即检查」永远写当前月，开始后自动切回「最新」。

## 单项刷新（只查一个文档）

用户问「某个文档这个月更新了吗」时，**不要跑整期**（整期要逐部门遍历共享树，约 5~15 分钟）。
工具入参 `doc_update_check(sync=false, name='供配电程序文档目录')`，`name` 传**文件名**：

| 命中 | 行为 |
|---|---|
| 唯一 | `sync=false` 读库按 `seq` 过滤（各部门的该项一起返回）；`sync=true` 只取匹配到的项（各部门的该项） |
| 多项（「隐患排查表」「中控室」） | 返回 `candidates`，**不下载**，让用户指明 |
| 0 项 | 返回 25 条清单 `available`，让用户改写措辞；不猜 |

匹配顺序（`catalog.resolve_items()`，先归一：去所有空白含全角空格、转小写）：

1. **编号**：`query` 等于某个 `number`，或以 `number + '.'` 开头（`1.6.6.2`）
2. **清单项名互相包含**：真实上传文件名含清单项名也命中
3. **消歧关键词**：`4.1.5制冷` → 四条里唯一的一条

合并口径（`fetch.merge_snapshot()` 纯函数，可单测）：部门分支按 `(dept_id, seq)` **整项替换**命中项，
其余项连同 `files[].last_changed_at` **原样保留**；未刷新的部门原样保留；`file_count` 重算，
`taken_at` 推进到本次，`snapshot_id`/`created_at` 不动。

**本期还没有整期快照时不落库**：拼到上期快照上做临时比对，返回 `stored=false` + 一句提示
（否则会造出只有少数项的"基线"，其余项全被算成「未找到」）。建议先跑一次整期。

本机核对（`--dept` 可再收窄到单个部门）：

```bash
python -m service.doc_update.cli --sync --name 供配电程序文档目录
python -m service.doc_update.cli --sync --name 能源月报 --dept A1
```

## 已知盲区

- **共享文件下载权限**：`/file/share/v1/minio/url/{fid}` 当前对服务账号 403 →
  只影响**原地更新型**（降级为 `failed` + 权限提示）；按期提交型不下载、不受影响。
  由管理员开通服务账号共享文件下载权限后自动恢复。
- 两次快照之间**改了又改回去**检测不到（哈希相同）
- 对象存储里若是**转换产物**，hash 跟踪的是产物，不是原始上传件
- 预签名地址有时效，单文件失败只记 `error`，不中断整轮
- 安全阀 `DOC_UPDATE_MAX_FILES=1000`（仅原地更新型才下载；按期提交型不占额度），
  超出的文件记「安全阀，未取」不中断

## 实现位置

| 位置 | 职责 |
|---|---|
| `service/doc_update/catalog.py` | 36 条清单常量，`MONTHLY_ITEMS` 为 25 条月度项；`folders` 登记实测真实目录名 |
| `service/doc_update/depts.py` | 固定清单 `configured_names()` + Mongo 注册表缓存（`ensure_registry` / `dept_columns` / `resolve_departments`）；`list_departments()` 只用于解析 `dept_id` |
| `service/doc_update/share.py` | `list_dir()` / `walk()` 共享树递归 + 编号剪枝；`download_sha256()` |
| `service/doc_update/period.py` | 纯函数：期号解析、目录形态判定、应到期、最新期 |
| `service/doc_update/fetch.py` | `take_dept_snapshot()`（部门分支）/ 旧 `take_snapshot()`（回滚）；`merge_snapshot()` |
| `service/doc_update/store.py` | Mongo `doc_update_snapshots`（同 `period_key` 覆盖；上月那份永不被写）；`previous()`（严格早于）、`list_periods()` |
| `service/doc_update/verdict.py` | `evaluate()` 分派部门/单树；两种判据 + 计数/变更/待复核 |
| `service/doc_update/workflow.py` | `perform_sync()` / `build_overview(period_key=None)`（始终带 `dept_columns` + `catalog` + `periods`）/ `ensure_initial_sync()`（显式开启自动基线且从无部门快照时跑一次）/ `refresh_items()` |
| `service/doc_update/sync_state.py` | 同步租约：`acquire()` / `Lease.update()` / `release()` / `read()`（TTL 120s、30s 心跳、Lua 比对 token、旧值容错） |
| `service/doc_update/presets.py` | 系统预设 `doc_update__monthly`（每月 10 日 03:00） |
| `service/doc_update/cli.py` | `--sync [--name X] [--dept A1] [--yes]`、`--period YYYY-MM`（历史期）、`--depts`（只看列）、`--refresh-depts`（重解析 id/根，需 `--yes` 写库） |
| `tools/doc_update_tools.py` | 对话工具 `doc_update_check(sync=false, name='')`（payload 带部门维度） |
| `skills/doc_update_check/SKILL.md` | 技能正文（口径/边界） |
| `api/v1/scheduled_tasks.py` | `GET /doc-update/overview?period=YYYY-MM`（回 `periods`/`missing_period`/`progress`）、`POST /doc-update/sync`（心跳租约 TTL 120s + 30s 续期） |
| `web/src/components/doc-update-matrix.tsx` | 旧矩阵（行 = 清单项、列 = 部门）；**已不再被引用**，仅保留文件 |
| `web/src/components/doc-update-status.ts` | 两处共用的状态色表 / `deptKey` / `countStatus` / `fmtTime` |
| `web/src/components/doc-update-panel.tsx` | 合规检查页顶部面板：25 项列表（部门/期间筛选）+ 立即检查；持有筛选状态 |
| `web/src/components/doc-update-board.tsx` | 整屏看板：同一份 6 列列表（点行展开文件明细），部门/期间筛选由面板托管 |
| `main.py` | `DOC_UPDATE_CHECK_ENABLED` + `DOC_UPDATE_AUTO_BASELINE` 且**从没有过部门口径快照**时，后台跑一次首期基线（与手动同步共用 Redis 租约） |

## 开关与运维

```dotenv
DOC_UPDATE_CHECK_ENABLED=true        # 开启：建每月预设 + 页面/工具可用
DOC_UPDATE_AUTO_BASELINE=false       # 启动时自动跑首期基线（默认关；开启后也只跑一次）
DOC_UPDATE_SCOPE=dept                # dept = 逐部门共享文档（默认）；tree = 旧单树（回滚用）
DOC_UPDATE_COMPANY_ID=27WqDiazDVr    # 旧格式/ tree 模式的默认公司（润泽）
# 固定「公司 × 部门」清单：`标签:companyId:部门,…`，组间 `;`（两段时标签用 id）。
# 不带 `:` 的旧值仍按单公司套 DOC_UPDATE_COMPANY_ID。
DOC_UPDATE_DEPTS='廊坊:27WqDiazDVr:A1,A2,A3,A5,A6,A7,A8,A9,A10,A11,A12,A18;平湖:27gIIjyTQ7n:A1,A2;惠州:27gIKJhwwZX:A2,A3;佛山:27gIL8vGj8F:A1,A2,A3;重庆:27oHS2fq6yp:A1,A2'
DOC_UPDATE_DEPT_PATTERN=^A\d+$       # 从接口行里认部门名（不作列清单）
DOC_UPDATE_PERIOD_GRACE_MONTHS=1     # 本期应到 = 当前月回退 N 月
DOC_UPDATE_TREE_ID=24CiSBO7cxt       # 仅 DOC_UPDATE_SCOPE=tree 时使用
DOC_UPDATE_CRON=0 3 10 * *           # 每月 10 日 03:00
DOC_UPDATE_TIMEOUT=30                # 单文件下载超时（秒）
DOC_UPDATE_MAX_FILES=1000            # 安全阀（仅原地更新型才下载）
```

依赖 `MGMT_*`（复用 mgmt 登录与文件接口）。关闭时不建预设、页面不渲染面板、工具不注册。

**启动默认不跑首期基线**：只有 `DOC_UPDATE_CHECK_ENABLED=true` 且 `DOC_UPDATE_AUTO_BASELINE=true`、
并且**从没有过部门口径快照**时，`main.py` 才会在后台跑一次（与手动同步共用 `doc_update:sync` 租约，多 worker 只有一个真正执行）。
日常首期基线由页面「立即检查」建立；之后由 `doc_update__monthly` 预设每月 10 日自动比对
（预设随 `SCHEDULED_TASKS_ENABLED=true` 注册并启动调度器）。

最初判据是「本期无快照」，导致每个新月份、每次 reload/部署重启都静默跑一遍 21 部门 × 25 项，
用户没点过页面也一直显示「检查中」——故改为默认关闭 + 「从无部门快照」判据。

**同步租约**：`doc_update:sync` 存 JSON（`token`/`mode`/`user_id`/`started_at`/`done`/`total`/`current`），
TTL 120s、后台每 30s 续期。进程被 kill 时 `finally` 不执行，租约也会在 ≤2 分钟内自然过期，
页面自动恢复「立即检查」；续期与释放用 Lua 比对 token，旧任务不会删掉新租约。

本机核对：

```bash
python -m service.doc_update.cli                 # 只看库里最新一期
python -m service.doc_update.cli --period 2026-10  # 看历史期（只读）
python -m service.doc_update.cli --depts         # 只看固定部门列（不联网、不看快照）
python -m service.doc_update.cli --refresh-depts # 重解析 dept_id/root_fid（加 --yes 才写缓存）
python -m service.doc_update.cli --company 平湖 --dept A1   # 按公司 + 部门收窄
python -m service.doc_update.cli --dept A1       # 只看 A1 部门（同名部门跨公司一起命中）
python -m service.doc_update.cli --sync --dept A1  # 单部门实跑，确认共享下载权限
python -m service.doc_update.tests.test_period      # 期号解析/形态判定
python -m service.doc_update.tests.test_dept_scope  # 部门分支状态判定
python -m service.doc_update.tests.test_verdict     # 旧单树分支零回归
python -m service.doc_update.tests.test_catalog
python -m service.doc_update.tests.test_merge
python -m service.doc_update.tests.test_depts_registry  # 固定清单/缓存合并（纯 assert）
python -m service.doc_update.tests.test_sync_state      # 租约互斥/进度/释放/token 防误删/旧值容错
```
