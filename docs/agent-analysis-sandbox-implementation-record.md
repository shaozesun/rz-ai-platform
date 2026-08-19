# Agent 数据分析沙箱实现记录

> 日期：2026-08-12 | 此文档供后续新对话理解本次改动全貌及溯源

---

## 一、背景与动机

Agent 在回答运维问题时，遇到需要**全量/大量数据**的统计分析需求（如「统计所有工单状态分布」），直接把平台能力的结果搬进 LLM 上下文，触发了两个问题：

1. **400 context-too-long**：LLM 在调用平台工具时可能填入过大的 `page_size`（实测传过 `204`），工具一次性返回 204 条完整记录，单条 ToolMessage 约 26 万 token，超出模型 262144 token 窗口，整轮对话直接 400 失败。
2. **全量数据无法分析**：即使不触发 400，几百条记录搬进上下文既不可行（成本/窗口），也没法让 LLM 对全量做聚合、分布、趋势等统计——LLM 读文本重建数字极易失真。

### 业界调研结论

调研了主流数据分析沙箱方案后，结论如下：

| 方案 | 形态 | 结论 |
|------|------|------|
| DifySandbox | 本地 Docker + Seccomp 受限容器 | 可私有化、企业标准；对本项目是**硬化目标** |
| E2B / Modal | 云端沙箱 | 数据出内网，不适用 |
| DeerFlow | code-execution（Local / Aio / E2B / Boxlite 多个 Provider） | 认可「沙箱执行」模式；本地受限执行器最贴合 |
| Boxlite | 本机 libsandbox | 需 root / 平台限制 |

结论：对任意 ad-hoc 分析，**受限本地执行器 + 数据物化**是最合适、可私有化的选择。当前 MVP 用「子进程 + 资源限制」作为执行边界，硬化路径为 Docker + Seccomp（对齐 DifySandbox）。

### 核心决策

| 决策 | 结论 | 原因 |
|------|------|------|
| 数据大小与上下文解耦 | 全量数据物化到磁盘，**原始数据永不进 LLM 上下文** | 从根上消除 400，成本可控 |
| 执行边界 | 受限本地子进程（`python -I` + RLIMIT + 禁网 + 超时 kill） | 内部可信环境 MVP 足够；Docker+Seccomp 为后续硬化路径 |
| 分析入口 | 两个 async 工具（`analysis_load` / `analysis_exec`），走 raw_tools | 天然 async，不经同步异常包装 |
| 渲染分工 | 沙箱负责「算得准」，AntV 负责「画得对」 | 渲染层零改动，LLM 原样贴 `<chart>` |
| 安全开关 | `AGENT_ANALYSIS_ENABLED=false` 默认关闭 | 代码可能随时发布生产，必须零影响 |

---

## 二、安全开关与配置

### 2.1 开关

所有分析代码由 `config/settings.py` 中 `AGENT_ANALYSIS_ENABLED` 控制：

```
AGENT_ANALYSIS_ENABLED=false   →  默认关闭，工具不注册，零开销
AGENT_ANALYSIS_ENABLED=true    →  开发/测试环境开启，注册 analysis_load / analysis_exec
```

### 2.2 配置项

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `AGENT_ANALYSIS_ENABLED` | `false` | 总开关 |
| `AGENT_ANALYSIS_DATA_DIR` | `data/analysis/` | 数据集落盘根目录 |
| `AGENT_ANALYSIS_TIMEOUT` | `30` | 单次代码执行超时（秒） |
| `AGENT_ANALYSIS_MAX_ROWS` | `10000` | 单数据集最大行数（全量拉取上限） |
| `AGENT_ANALYSIS_PAGE_SIZE` | `100` | 拉取平台数据的分页大小 |
| `AGENT_ANALYSIS_MAX_MEMORY_MB` | `512` | 沙箱子进程内存上限（RLIMIT_AS） |
| `AGENT_ANALYSIS_MAX_OUTPUT_CHARS` | `4000` | 单次执行返回结果上限 |
| `AGENT_ANALYSIS_MAX_DATASETS_PER_SESSION` | `20` | 每会话数据集数上限，超限删最旧 |

另新增工具层封顶配置（修 400 context-too-long，见 4.1 节）：

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `AGENT_MAX_PAGE_SIZE` | `20` | 平台工具 `page_size` 参数上限（LLM 乱填时 clamp） |
| `AGENT_MAX_TOOL_OUTPUT_CHARS` | `12000` | 单条工具结果进上下文前截断上限（紧凑序列化后全量放得下） |

---

## 三、文件改动清单

### 3.1 新建文件（4 个）

#### core/sandbox/ — 分析沙箱核心

| 文件 | 行数 | 说明 |
|------|------|------|
| `core/sandbox/__init__.py` | 1 | 包标记 |
| `core/sandbox/runner.py` | 179 | 受限子进程执行器 `run_python()` — 隔离执行 + 资源限制 + chart_data 提取 |
| `core/sandbox/datasets.py` | 138 | 数据集存储 — `save_dataset()` 落盘 / `load_dataset_dir()` 读取 / 会话隔离与淘汰 |

#### tools/ — Agent 工具

| 文件 | 行数 | 说明 |
|------|------|------|
| `tools/analysis_tools.py` | 192 | 两个 async 工具：`analysis_load`（全量物化）+ `analysis_exec`（沙箱 pandas 分析） |

### 3.2 修改文件（3 个）

| 文件 | 改动位置 | 改动内容 |
|------|---------|---------|
| `config/settings.py` | L246-258 | 新增 `AGENT_MAX_PAGE_SIZE` / `AGENT_MAX_TOOL_OUTPUT_CHARS` + 8 个 `AGENT_ANALYSIS_*` 配置 |
| `service/query/base/tools_factory.py` | `_run()` | clamp `page_size`/`pageSize` 到上限 + 紧凑序列化 + 记录级截断（完整记录）+ 字符级兜底，截断标记引导分页/沙箱 |
| `service/agent/agent_service.py` | `_ensure_agent()` | 按 `AGENT_ANALYSIS_ENABLED` 构建 `analysis_tools`，并入 `raw_tools`（与 deferred_tools 并列） |

---

## 四、架构设计要点

### 4.1 工具层封顶（修 400 context-too-long）

**根因**：`tools_factory.py` 生成的平台工具把 LLM 传入的 `page_size` 原样透传给后端，LLM 填 `204` → 返回 204 条完整记录 → 单条 ToolMessage 约 26 万 token，超窗口 400。

**修复**：`_run()` 内三层保护——
1. **clamp 分页**：`page_size`/`pageSize` 超过 `AGENT_MAX_PAGE_SIZE(20)` 一律压回 20；
2. **紧凑序列化**：`separators=(',', ':')` 去 indent，同预算装更多完整记录；
3. **记录级截断**：超预算时弹掉末尾**完整记录**（不按字符硬切，避免「只显示首条 +
   半截字段」），字符级仅作单条极宽记录兜底；截断标记引导 LLM 分页补查，沙箱开启时
   额外提示调用 `analysis_load` 物化全量 + `analysis_exec` 分析。

### 4.2 数据流（analysis_load → analysis_exec）

```
用户：「统计所有工单状态分布，画个饼图」
  │
  ▼
analysis_load(capability_id, params)           tools/analysis_tools.py
  │  ① 校验能力（get_capability/get_caller）
  │  ② 分页拉【全量】数据（绕过 tools_factory 的 page_size 封顶，
  │     受 AGENT_ANALYSIS_MAX_ROWS 保护；_extract_records/_extract_total 通用抽取）
  │  ③ save_dataset() 落盘为 {DATA_DIR}/{thread_id}/{dataset_id}/(data.csv + meta.json)
  │  ④ 只回摘要 {dataset_id, rows, columns, preview, truncated}
  │     ── 原始数据永不进 LLM 上下文 ──
  ▼
analysis_exec(dataset_id, python_code)         tools/analysis_tools.py
  │  ① load_dataset_dir() 定位数据集
  │  ② run_python() 受限子进程执行 pandas 代码（预载 df = read_csv('data.csv')）
  │  ③ 返回 {result: 文字输出, chart_data?: 结构化图表数据}
  ▼
LLM 组装 <chart type="..." attrs="..." data='[chart_data 原样]'/>
  ▼
ChartRenderer（AntV：@ant-design/plots + @ant-design/graphs）前端本地渲染

（可选）用户要「看板/大屏」时改走 HTML 看板并行路径（见 4.7）：
  analysis_write_file('dashboard.html') → analysis_exec(file='dashboard.html')
  → artifacts 回传 → LLM 输出 <file url name="dashboard.html"/>
  → 前端 ArtifactPanel / HtmlPreview iframe 本地预览
```

### 4.3 沙箱安全边界（runner.py）

| 层 | 手段 | 防什么 |
|----|------|--------|
| 解释器 | `python -I` 隔离模式 | 忽略 PYTHONPATH / user site，环境不泄漏 |
| 工作目录 | cwd 锁定数据集目录 | 只能读到当前数据集 data.csv |
| 内存 | `RLIMIT_AS`（512MB 上限） | 恶意/失控代码无限申请内存 |
| CPU | `RLIMIT_CPU`（timeout 上限） | 死循环占用 CPU |
| 网络 | `socket.socket` / `urllib` 置为抛异常 | 数据外泄、SSRF |
| 超时 | `asyncio.wait_for` + `proc.kill()` | 无限挂起 |

安全定位：面向内部可信环境（内部运维数据）的 MVP，代码逃逸面受子进程 + 资源限制约束；硬化路径为 Docker 容器 + Seccomp（对齐 DifySandbox）。

### 4.4 chart_data 链路（增强）

沙箱不仅能算，还能直接产出**结构化图表数据**，LLM 原样贴进 `<chart>`，不再手写重建数字：

```
用户 pandas 代码把结果赋给变量 `result`（DataFrame / Series / list[dict] / dict）
  → wrapper 模板提取：DataFrame/Series 转 json records，其余 json 序列化校验
  → 写入 cwd/_chart_data.json
  → runner 读回：超 max_output_chars 则置 None + note（提示先缩小聚合）；读后删除文件
  → analysis_exec 返回 {result, chart_data?, note?}
  → LLM 组装 <chart> 时原样引用 chart_data，禁止改写数值
```

约定（写进工具 description 告知 LLM）：
- `print()` 输出文字结果；需要图表数据时把最终结果赋给 `result` 变量，两者可并存；
- `chart_data` 是沙箱原样产出的 JSON，LLM 组装 `<chart>` 标签时原样引用，**禁止改写数值**。

> HTML 看板是另一条**并行产物路径**：`analysis_write_file` 写自包含 `.html` → `analysis_exec(file=...)` 执行 → `<file>` 产物（不走 chart_data，见 4.7）。

### 4.5 工具通道与会话隔离

- 两个分析工具是 **async `@tool`**，走 `raw_tools` 通道（与 deferred 平台工具并列），**不经** sync 异常中间件包装——天然 async，且自带错误兜底（返回 `{error}` JSON）。
- 通过工具签名里的 `config: RunnableConfig = None` 注入，`_get_thread_id(config)` 从 LangGraph config 取 `thread_id`，数据集按 thread_id 会话隔离。
- 每会话数据集数量受 `AGENT_ANALYSIS_MAX_DATASETS_PER_SESSION` 约束，超限自动删最旧（`_evict_if_needed()`）。

### 4.6 数据集目录结构

```
data/analysis/
  └── {thread_id}/                     ← 会话隔离（thread_id 安全化）
        └── {dataset_id}/              ← 如 data-a1b2c3d4
              ├── data.csv             ← 全量记录（沙箱子进程 cwd 读取）
              └── meta.json            ← 来源能力/参数/行数/列名/时间
```

### 4.7 HTML 看板扩展（自包含文件 + 前端预览）

用户**明确要「看板/驾驶舱/大屏/HTML 报表」**时，Agent 走另一条**并行产物路径**——生成自包含 HTML 看板文件，前端右侧面板本地渲染「迷你看板」（不出内网）：

```
用户要「看板」→ analysis_load 物化数据 → skill.html_dashboard.dashboard_template 取模板
  → analysis_write_file('dashboard.html') 写自包含 HTML（数据内联 <script id="data"> + echarts.init，无外部 src）
  → analysis_exec(file='dashboard.html') 执行 → artifacts 回传（api/v1/agent.py 已加 .html → text/html）
  → LLM 输出 <file url="..." name="dashboard.html"/>
  → 前端 ArtifactPanel.tsx：HtmlPreview.tsx 拉 blob → srcdoc = 打包内联 echarts 源码 + 页面 HTML
    → <iframe sandbox="allow-scripts"> 本地渲染（echarts 不引 CDN、数据不出内网）
```

- 看板规范在 `core/agent/prompts.py`「HTML 看板生成规范」；模板与 echarts 用法见 `skills/html_dashboard/references/dashboard_template.md`
- **安全**：iframe 无 allow-same-origin + 页面 CSP `default-src 'none'` → agent 生成的 HTML 无法读父页面、无法外发请求；`.html` 产物仍强制附件下载（`_ACTIVE_CONTENT_TYPES`），预览走前端 blob fetch 不受 disposition 影响

---

## 五、验证命令

```bash
# 1. 装配不回归
python -c "import main" && echo OK

# 2. runner 单测：result=DataFrame → chart_data 正确；无 result → None
python - <<'PY'
import asyncio, tempfile, os
import pandas as pd
from core.sandbox.runner import run_python
tmp = tempfile.mkdtemp()
pd.DataFrame([{'status':'a'},{'status':'b'},{'status':'a'}]).to_csv(os.path.join(tmp,'data.csv'), index=False)
async def m():
    r = await run_python("result = df.groupby('status').size().reset_index()", cwd=tmp, timeout=10, max_output_chars=1000, max_memory_mb=512)
    print('chart_data:', r['chart_data'])
    r2 = await run_python("print('no result')", cwd=tmp, timeout=10, max_output_chars=1000, max_memory_mb=512)
    print('无 result:', r2['chart_data'])
asyncio.run(m())
PY

# 3. 集成：问「统计所有工单状态分布，画个饼图」→ analysis_exec 返回 chart_data
#    → LLM 原样贴进 <chart> → AntV 渲染，图表数值与沙箱结果逐字节一致
cd web && npm run dev
```

---

## 六、架构全景图

```
┌─────────────────────────────────────────────────────────────────────┐
│                       rz-ai-platform (Agent 模块)                    │
│                                                                      │
│  tools/analysis_tools.py      ← 新增 2 个 async 工具                 │
│  ├── analysis_load  全量数据物化（分页拉全量 → 只回摘要）            │
│  └── analysis_exec  沙箱 pandas 执行（返回 result + chart_data）     │
│            │  async，走 raw_tools（不经 sync 异常包装）              │
│            ▼                                                         │
│  core/sandbox/                    ← 新增分析沙箱核心                 │
│  ├── datasets.py    save_dataset / load_dataset_dir / 会话隔离淘汰   │
│  └── runner.py      run_python() 受限子进程执行器                    │
│            │  python -I + RLIMIT_AS/CPU + 禁网 + 超时 kill           │
│            ▼                                                         │
│  data/analysis/{thread_id}/{dataset_id}/data.csv + meta.json         │
│            │                                                         │
│            ▼                                                         │
│  前端 ChatArea → <chart> 解析 → ChartRenderer (AntV) 本地渲染；      │
│  .html 看板 → ArtifactPanel / HtmlPreview（sandbox iframe + echarts 内联）│
│                                                                      │
│  service/agent/agent_service.py   ← 接线 analysis_tools → raw_tools  │
│  service/query/base/tools_factory.py ← 工具层封顶（修 400）          │
│  config/settings.py               ← AGENT_ANALYSIS_* + 封顶配置     │
│                                                                      │
│  扩展：HTML 看板（ArtifactPanel + skill.html_dashboard，见 4.7）     │
│  未改动：渲染链路（AntV）、平台能力注册（Capability/Caller）          │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 七、变更溯源

- 本次改动日期：2026-08-12
- 会话范围：rz-ai-platform Agent 可靠性 + 数据分析沙箱实现
- 关联对话记录：`/Users/shaoze/.claude/projects/-Users-shaoze-Downloads-rz-ai-platform/b697d063-da00-4563-9cd7-24adcc729c65.jsonl`
- 关联设计文档：
  - `docs/agent-architecture-design-v2.md` — Agent v2.0 方案设计
  - `docs/capability-catalog-scaling-design.md` — 能力目录扩容设计
  - `docs/agent-phase1-implementation-record.md` — Agent Phase 1 实现记录
- 业界参考：
  - DifySandbox — 受限执行器硬化目标（Docker + Seccomp）
  - DeerFlow code-execution — `/Users/shaoze/PycharmProjects/deer-flow/`（沙箱执行模式借鉴来源）
