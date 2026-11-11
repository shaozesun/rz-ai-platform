# Langfuse 大量记录显示为空（缺 trace 根）

日期：2026-10-31 首次记录；2026-11-02 更正根因
状态：代码已改，待部署验证

## 结论

不是记录丢失，是**大量 trace 缺根节点**。Langfuse 列表的 name / input / output 三列取的是
trace 根的字段，根没进库 → 三列全空；而根下面的 CHAIN / GENERATION / TOOL 观察数据完好，
点进去仍能看到完整的输入输出。

根缺失的直接原因：**根观察挂在一个「只开始、不结束」的父 span 上**（该 id 查
`GET /api/public/observations/{id}` 返回 404）。OTEL 只在 span `end()` 时导出，它永远进不了库；
Langfuse 找不到 trace 根，trace 就成了空壳。

**2026-10-31 的第一版根因（`aupdate_state` 带 callbacks）只对了一半**：它解释了 132 条
`LangGraphUpdateState` 噪音 trace，但不是新 trace 仍为空的来源。2026-11-02 复查新数据后
确认真正机制是**同步回调被派发到线程池，根 span 从环境 OTel 上下文取到了残留 span 作父节点**。

## 证据

### 第一轮（2026-10-31，历史数据）

- 672 条 trace：261 条 `LangGraph`（正常）、132 条 `LangGraphUpdateState`（噪音）、
  1 条手工测试、**278 条 name/input/output 全空**。
- 坏 trace `68e88590468d80ee77e0006900c9cc92`：129 条观察、0 条根观察；其中 `LangGraph`
  span 有 input 398 字、output 138,625 字、正常结束、metadata `is_langchain_root: true`，
  但 `parentObservationId = 7388daa31a6ac416` 查不到。
- 幽灵父 id 每条 trace 各不相同（7388…、9be8…、0801…、5f0e…、39df…），每个幽灵只有
  `LangGraph` 一个子节点 → 每次请求各自产生一个。

### 第二轮（2026-11-02，部署第一版修复之后的新数据）

用同一套公共 API（`pk/sk` 见 `deploy/config/.env.production`）核对：

- 共 696 条 trace；**09:09 / 09:10 / 09:13 / 09:15 四条新 trace 仍为空**。
- 这四条 trace 级 `sessionId` / `userId` **都有值** → 「metadata 键改名」那部分修复已生效。
- 其中 3 条能找到 `LangGraph` 根观察（`is_langchain_root=true`、input/output 完整），
  但 `parentObservationId` 分别是 `d520f5e2c049` / `64f9cd248f7e` / `6f5796ed5d36`，
  逐个查询均 404。
- 09:15 那条**没有任何根观察**，子观察挂在幽灵 `dfac8c4c8553` 下 —— 属于流中断型，
  见「边界」。

即：第一版修复后，噪音 trace 与「会话/用户」列恢复了，但**根节点归属没变**。

## 根因（源码核对，langfuse-python 5.0.4）

1. `langfuse.langchain.CallbackHandler` 是同步 `BaseCallbackHandler`，且没有覆盖
   `run_inline`（基类默认 `False`）。LangChain 因此把它的回调逐个丢到**线程池**执行：
   `langchain_core/callbacks/manager.py::_ahandle_event_for_handler` 里
   `loop.run_in_executor(None, copy_context().run, event, ...)`。
2. handler 用上下文变量维护「当前 span」：`_attach_observation()` 调
   `context.attach(trace.set_span_in_context(observation._otel_span))`；`on_chain_end` 时
   `_detach_observation()` 用**当次 attach 返回的 token** 调 `context.detach`。token 只在
   创建它的 context 里有效，跨线程/跨 context detach 会抛错，而
   `_detach_context_token_safely` **把这个错误吞掉**。结果部分 span 残留在执行回调的工作
   线程上下文里（线程池线程复用，残留会留给后续任务）。
3. 根 run（`parent_run_id is None`）走 `_take_root_trace_context()`：若 handler 没有显式
   `trace_context` 且「当前 span」有效，它返回 `None`；随后 `on_chain_start` 调
   `obs.start_observation(trace_context=None, ...)` → 客户端 `start_span(name)` **不传父上
   下文** → 父节点取自环境 OTel 当前 span，也就是上一步残留的那个 span。
4. 那个残留 span 从未 `end()`，永远不会导出 → 根观察的 `parentObservationId` 在库里查不到
   → Langfuse 找不到 trace 根 → 列表三列为空。

这也解释了为什么第一版修复无效：它处理的是 handler 共享状态与 `aupdate_state` 的噪音
（两者都已修好），但根节点归属仍然依赖**环境上下文**，而环境上下文在执行层就被污染了。

## 修复（2026-11-02）

`core/agent/tracing.py`：

1. **`run_inline = True`**（子类 `_InlineCallbackHandler`）：回调在事件循环的当前上下文里
   同步执行，attach / detach 成对落在同一个 context，不再往工作线程上下文里残留 span。
2. **显式 `trace_context={'trace_id': uuid.uuid4().hex}`**：`_take_root_trace_context()`
   的第一分支直接返回 `self._trace_context`，根 span 不再看环境 OTel 上下文，恒为本请求
   trace 的根（handler 每请求新建，所以每条对话一轮一个 trace）。

两层独立生效：`run_inline` 治本（上下文不再错配），`trace_context` 兜底（即使将来回调调度
方式再变，根节点也一定为根）。`service/agent/streaming.py` 只同步了参数注释，逻辑未动
（`reset_config` 不带 callbacks 的 `aupdate_state` 保留，它是纯状态副作用）。

## 验证（部署后）

查 Langfuse 公共 API（pk/sk 见 `deploy/config/.env.production`），连开 3~5 条新对话（含 2 条并发）：

1. 新 trace `name` 为 `LangGraph`、`input`/`output` 非空；
2. 根观察（`is_langchain_root=true`）的 `parentObservationId` 为 `null`；
3. 新 trace 的 `sessionId` / `userId` 有值；
4. 部署时刻之后不再新增 name 为空的 trace；
5. 并发会话之间没有互相串成父子。

## 边界

- **历史空 trace 不回填**（约 280 条）：根节点当时就没导出，补不回来；子观察数据都在，
  可用 `GET /api/public/traces/{id}` 取 observations。若要在列表里恢复 name/input/output，
  可用 `deploy/scripts/langfuse_backfill_traces.py` 按根观察的字段补写 trace 本体
  （默认 dry-run，只改 trace 字段、不动观察），需单独确认后执行。
- **流中断型空 trace**：客户端断开 / 流提前结束时，根 span 没有走到 `end()`，OTEL 不导出，
  trace 里可能连根观察都没有（如 09:15 那条）。本次修复不覆盖这一类。
- **依赖版本漂移**：`requirements.txt` 写的 `langfuse>=2.0.0` 与代码实际用法（4.x/5.x 的
  `langfuse.langchain`）严重不符，应先确认服务器实际版本再锁下界；本修复依赖 5.0.4 的
  `CallbackHandler(trace_context=...)` 构造参数。
- 本机未安装 `langfuse` 包，无法本地复现追踪链路；只能静态检查，实效果以部署后新 trace 为准。
- 前端 / HUD 不受影响：Agent 对话与产物链路与追踪完全解耦。
