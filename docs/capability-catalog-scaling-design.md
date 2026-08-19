# 能力注册与工具编排架构设计：四层分类 + 工具规模收敛

> **版本**：v1.1 | **日期**：2026-08-10 | **主题**：Capability 新增 category 层（四级），
> 修复「工具多了不好用」的 4 处全量暴露点
>
> 结论先行：当前「1 Capability → 1 工具 + deferred 按需加载」的方向是对的，问题不在 1:1，
> 而在 4 处「把全部暴露/返回出去」的地方（promoted 跨轮累积、`<available-tools>` 全量工具名、
> tool_search 返回完整 schema、检索无分类维度）。新增 category 四级层后，把「同时可见工具数」
> 收敛到**每请求**预 promote 的规模（promoted 每请求重置，不跨轮累积）；多数查询走意图快路径
> 直接命中，不再依赖 tool_search。

## 1. 背景与问题

### 1.1 现状

每条 Capability → 1 个 StructuredTool（`service/query/base/tools_factory.py`），挂在
deferred tool 机制下（`core/agent/deferred.py`）：schema 默认对 LLM 隐藏，靠 tool_search
语义检索按需加载。当前规模：dcim 6 + mgmt 13 = **19 个 deferred 工具**（mgmt 13
含按 uid 查人详情的 `query_person_by_id`，此前文档按 12 计，实际已 13）。

### 1.2 目标规模与「不好用」的根源

按 mgmt 56 controller（→ 约 10-15 个功能分类）+ dcim 100+ 只读能力规划，工具将到
**150-250 个**。此时当前机制有 4 处「把全部暴露出来」的地方，随工具数线性/超线性恶化：

| # | 问题 | 位置 | 100+ 工具时的影响 |
|---|---|---|---|
| P-1 | promoted 无上限累积 | `deferred.py:40-54` `merge_promoted` | 会话越长可见工具越多，schema 体积与选择难度都涨 |
| P-2 | `<available-tools>` 列全量工具名 | `deferred.py:216-226` `deferred_tools_prompt` | ~100 工具名 ≈ 3000+ token，**每一轮**都吃 |
| P-3 | tool_search 返回 8 个完整 OpenAI schema | `deferred.py:185-188` | 一次调用 ≈ 3000-6000 token 塞进上下文 |
| P-4 | 暴力余弦检索，无分类/域维度 | `retriever.py:60-78` `search_capabilities` | 跨域相似能力越多，top-k 越可能跑偏；与意图路由脱节 |

另有 2 个已知缺口放大了影响：
- **dcim 的 `intent_labels` 全空**（`dcim/capabilities.py`）→ 快路径（意图→绑定表→预
  promote）对 dcim 永不生效，恒走 tool_search 慢路径。
- **`domain` 是死字段**——只在元数据里，检索/路由都不读。

### 1.3 设计约束（对齐既有文档）

- `docs/external-integration-architecture.md` §3：「不做接口到工具的一对一映射（Agent 工具
  超 ~20-30 个即准确率骤降）」。**本设计立场**：20-30 上限应理解为「**同时可见工具数**」
  而非「登记工具总数」。deferred 机制已把「同时可见」压得很小，方向正确；要修的是上面
  4 处「暴露/返回全量」的地方。登记 150-250 个工具本身不降准，降准的是同时塞给 LLM 太多。
- **选型定位（路由为主、检索兜底）**：主路径是**意图路由**——`plan_query` 让 LLM 粗分类，
  `route_to_tools` 查 `intent_bindings` 确定性绑定表（label → 能力列表）整组预 promote，
  不经过相似度检索。`tool_search`（embedding+余弦 top-k）只是兜底。主流「工具越多 topk
  检索越不准」的担忧针对把检索当主力的架构，对我们主路径不构成威胁——路由是查表，能力从
  19 涨到 200 查表成本不变；只有兜底那一路的余弦计算线性涨（可忽略）。
- **分类粒度约束（新原则）**：一个分类下的能力数适中（目标 ~5-8 个），保证单次查询
  「按分类预 promote」给出的工具组不大不小。该约束作用于**单请求的可见集**（每请求重置
  后），与跨轮累积无关。mgmt 56 controller / ~12 分类 ≈ 平均 4-5；dcim 100+ / ~20 分类
  ≈ 5-7，天然满足。

## 2. 目标建模：四级树（平台 → 分类 → 域 → 能力）

与用户认知模型「平台-功能分类-域-函数」对齐：

```
mgmt（平台）
 ├─ org 组织人事（分类）           ── device 设备（分类）        ── work_order 工单…
 │   └─ person 人员（域）               └─ device 设备（域）
 │       ├─ query_person_by_name              ├─ query_device_list
 │       └─ query_person_by_id                └─ query_device_detail
```

**数据模型改动（最小）**：`Capability` 新增 `category: str`（L2 功能分类，闭合词汇表、按
平台定义）；现有 `domain`（L3）保留、成为分类下的子层。**id / 工具名 / caller 契约 / 平台
注册机制全部不变**（如 `mgmt.query_person_by_name` 工具名为 `mgmt__query_person_by_name`），category 只进元数据。

```python
# service/query/base/capability.py
@dataclass(frozen=True)
class Capability:
  id: str            # 'mgmt.query_person_by_name'（工具名/检索都依赖它）
  name: str
  description: str
  params: dict[str, str]
  category: str = ''      # 新增 L2 功能分类，如 'org'/'device'/'alarm'；按平台定义
  domain: str = ''        # L3 域，分类下的子层，如 'person'/'fault'
  kind: str = ''
  intent_labels: tuple[str, ...] = ()   # 与 category 合并为同一套词汇（见 §3.5）
  required_perm: str = ''
```

**分类表（落地时按 mgmt 56 controller 补全，此表为起点）**：

| 平台 | 分类（category） | 覆盖域（domain） | 当前能力 |
|---|---|---|---|
| mgmt | org 组织人事 | person | query_person_by_name / query_person_by_id / query_dept_tree |
| mgmt | device 设备 | device / fault | query_device_list / query_device_detail / query_device_fault |
| mgmt | work_order 工单 | work_order | query_maintain_order |
| mgmt | inspection 巡检 | inspection | query_inspection_order / query_inspection_detail |
| mgmt | risk 隐患 | risk | query_risk_list / query_risk_detail |
| mgmt | report 周报 | report | query_weekly_report |
| dcim | alarm 告警 | alarm | query_alarm / query_alarm_history |
| dcim | asset 资产 | asset | query_asset / query_asset_count |
| dcim | capacity 容量 | capacity | query_capacity / query_power_usage |

## 3. 修复设计

### 3.1 P-1：promoted 无上限累积 → 每请求重置（无状态路由）

`promoted` 是 LangGraph 的 state channel，带 reducer（`merge_promoted` 纯并集）后跨轮
**累积**（checkpoint 按 `thread_id` 持久化）。修复不做「上限+淘汰」，而是**每条请求开始
时重置为 None**——与主流「每请求无状态重路由」一致，且更简单：

```python
# service/agent/streaming.py stream_agent() 开头（或 AgentService.stream）
# 注意：不能传 {'promoted': None} —— merge_promoted reducer 把空值当「节点未触碰」
# 保留旧值；必须用空 hash 哨兵才能真正清空（已实测确认）。
agent.update_state(config, {'promoted': {'catalog_hash': '', 'names': []}})
```

- **只重置生命周期，不动搜索**：`tool_search` 的语义检索、发现卡片、`merge_promoted`
  union 逻辑全部原样。重置发生在请求边界，**当轮内** `tool_search → 调用` 的累积不受影响。
- 运维查询是离散短查询，跨轮不需要工具持续在场；同域追问由每轮意图路由（`route_to_tools`
  重新预 promote）兜底，不依赖跨轮 promote。
- 多用户共享后端 + MemorySaver 进程内存 checkpoint（重启即失），会话级 promote 语义本就
  不可靠，无状态反而是更稳的默认。
- 兜底：被重置的工具若被 LLM 调用，middleware 返回「请先 tool_search」，LLM 可重搜，可恢复。
- 不新增配置项（无 `AGENT_PROMOTED_MAX`）。

### 3.2 P-2：`<available-tools>` 改为分类清单

LLM 不需要提前知道确切工具名——tool_search 是语义检索，按自然语言描述意图即可命中。清单
只需告诉它「有哪些分类、各覆盖什么」，引导它组织搜索 query：

```
<available-tools>
平台工具默认隐藏，需先调用 tool_search 加载参数定义后再调用。用自然语言描述要查的内容（如「查郭春磊的部门」「A7 机房告警」）。
- mgmt（综合管理平台）：组织人事(人员/部门) / 设备(台账/故障) / 工单(维护/任务) / 巡检 / 隐患 / 周报
- dcim（数据中心）：告警 / 资产 / 容量
</available-tools>
```

- 新 helper `build_category_manifest()`（放 `service/query/base/`，从 `all_capabilities()`
  按 platform→category→domain 聚合），`deferred_tools_prompt` 改为接收并渲染 manifest。
- token：2 平台 × 6 分类 ≈ 250 token，vs 现 100 工具名 ≈ 3000+。**每轮省 ~2.7k token**。

### 3.3 P-3：tool_search 返回「发现卡片」而非完整 schema

关键观察：**被 promote 的工具，下一轮 model request 里 LangGraph 会自动带上它的完整
schema**（middleware 不再隐藏它）。所以 tool_search 的 ToolMessage 无需重复完整 schema——
它只告诉 LLM「我发现了什么、你可以选哪个」。改为紧凑卡片：

```python
content = json.dumps(
  [{'name': t.name, 'description': _truncate(t.description, 80),
    'params': list(t.args_schema.model_fields.keys())} for t in matched],
  ensure_ascii=False, indent=2,
)
```

- 8 个工具 ≈ 150-250 token，vs 现 3000-6000。参数详说明下一轮 model request 里有，不丢信息。

### 3.4 P-4：检索加分类/域维度 + 快慢路径打通

- `search_capabilities(query, k=8, *, category=None, domain=None)`：索引侧把 `_CACHE_IDS`
  扩成 `id → (platform, category, domain)` 桶，给定 category/domain 时先过滤再余弦。计算量
  不变（embedding 才是大头），主要提升**跨域准确率**与**可扩展性**。
- **快慢路径打通（核心）**：意图识别产出的分类本来就在 `IntentPlan.intent_labels` 里，现有
  `route_to_tools`（`orchestrator.py:24-69`）已经在按标签预 promote。本轮把快路径做实
  （见 §3.5），多数查询直接命中、根本不走 tool_search——这是「接口多了不好用」的根治。
- tool_search 维持全局语义检索即可；若后续要更细，可把当轮路由命中的分类经 `InjectedState`
  传入 tool_search 做偏置（记录为后续增强，需加 state schema 管线，当前不划算）。

### 3.5 快路径做实：intent_labels 与 category 合并

- `build_intent_bindings()`（`intent_bindings.py:14-24`）改为：能力 `intent_labels` 为空时
  **回退用 `category`** 作为绑定标签 → 绑定表 = category → 能力列表。只维护 category 一套
  词汇，不再有两套漂移。
- dcim 6 条能力回填 `category`（alarm/asset/capacity）→ 快路径对 dcim 生效。
- `all_intent_labels()` 自动变为分类词汇表，`query_planner` 的 Literal 约束随之生效。
- 效果：查询命中 1-2 个分类 → 每请求预 promote 5-12 个工具（受分类粒度约束，见 §1.3），
  LLM 直接调，多数场景不再触发 tool_search。

### 3.6 数据模型与工具名不变

id / 工具名 / caller 契约 / 平台注册机制全部不动。改动收敛在 base 层 + 能力声明文件。

## 4. 落地步骤（文档给出指引，本轮不写代码）

1. `service/query/base/capability.py` — 加 `category: str = ''`。
2. `service/query/mgmt/capabilities.py`、`service/query/dcim/capabilities.py` — 逐条填
   `category`（按 §2 表格）；新增能力声明时 `category` 必填。
3. `service/query/base/intent_bindings.py` — 空 `intent_labels` 回退 `category`。
4. `service/query/base/retriever.py` — 桶化索引 + `category`/`domain` 过滤参数。
5. `service/agent/streaming.py` — `stream_agent()` 开头重置 promoted（`agent.update_state(config,
   {'promoted': None})`）；`merge_promoted` 的 union 逻辑原样保留（当轮内 tool_search 累积正常）。
6. `core/agent/deferred.py` — ① `build_tool_search` 返回发现卡片；② `deferred_tools_prompt`
   改收 manifest（新 helper `build_category_manifest` 放 `service/query/base/`）。
7. `service/agent/agent_service.py` — 传 manifest 给 prompt。
8. `config/settings.py` — 无新增配置（promoted 重置不需要配置项）。

## 5. 验证（文档交付后执行）

1. 单元：`search_capabilities('告警', category='alarm')` 只返回 alarm 域。
2. token 测量：打印 tool_search ToolMessage 与 `<available-tools>` 字符数，对比修前/修后。
3. 端到端：`AGENT_ENABLED+MGMT_ENABLED` 问「郭春磊是哪个部门的」→ 快路径预 promote org
   分类，SSE `tool_call` 命中 `mgmt__query_person_by_name` 且**无 tool_search 调用**；问
   「A7 机房告警 + 郭春磊电话」→ parallel 预 promote alarm+org 两组。
4. 无状态验证：同一 thread 连续跨多个分类提问，断言第二轮开始前 promoted 已被重置（读
   checkpoint），每轮可见工具数 = 当轮预 promote + 当轮 tool_search 增量，不跨轮累积。

## 6. 后续增强（记录，不实现）

- tool_search 按当轮路由分类偏置（§3.4）。
- manifest / 检索按 `required_perm` 过滤，与 RBAC 结合。
- 开发期校验：新增能力 `category` 必填，漏填直接报错暴露。
