# E.V 插件系统对齐 deepseek-harness Agent 设计 —— 工具调用延迟优化方案

> 对照参考：[deepseek-ai/deepseek-harness](https://github.com/deepseek-ai/deepseek-harness) 的 `agent-loop` / `tools` / `llm-streaming` 子系统。
>
> 本文档面向 E.V 5.0 之后的迭代，**不**修改已稳定的直播链路，只动 `plugins/` 框架层 + `ev/llm/brain/` 工具调度层。

---

## 0. 摘要

E.V 现在已经完成 4 件事（与 dsh 设计同向）：

- 消息生命周期 17 个钩子 + PluginContext —— dsh 用 `agent/*` 事件总线实现
- 工具（Function Calling）动态聚合 + 热注册 —— dsh 用 `ctx.tools` 注册表
- 事件总线 + 系统提示补丁 —— dsh 用 `ctx.emit` / `ctx.effect`
- 自动发现 + 热重载 —— dsh 用 `ctx.effect()` 反向卸载

差距集中在**工具调用管线**。本文先列 12 项 dsh 已有、E.V 没有的设计点（按延迟影响排序），再给落地路径。

---

## 1. E.V 工具调用当前管线（用 dsh 的视角看）

E.V 当前的工具执行位置：`ev/llm/brain/chat/inner_loop.py:283` → `_execute_tool_calls`（`ev/llm/tools/executor.py:17`）。

```
LLM stream 结束（stream_drainer 跑完）
        ↓
_parse_qwen_tool_calls 解析（同步，CPU）
        ↓
_execute_tool_calls(mcp, tool_calls)
        ↓   asyncio.gather(*(_run(tc) for tc in tool_calls))
        ↓   每个 _run：call_tool → 失败 sleep 1s → 1 次重试
        ↓   拼装 tool 消息
下一轮 LLM 请求发出
```

对比 dsh 的管线（`docs/tool-execution-pipeline.md`）：

```
LLM 增量输出（assistant/chunk*）
        ↓ 每收到一个 tool-call 块
tool/call（durable 日志）
        ↓
tools/pre-execute waterfall（policy / approval / sandbox，可短路拒绝）
        ↓
monotonic guards（最终拒绝位）
        ↓
tools/execute waterfall（timeout / retry / metrics around-dispatch）
        ↓
tool body（cooperative cancel via exec.signal）
        ↓
tools/post-execute waterfall（accept / block / replace / add context）
        ↓
output.render + finalizeContent
        ↓
tool/result（durable 日志）
        ↓
barrier + rolling-pool：模型可立即请求下一轮（不等所有工具完成才发）
```

**E.V 当前是"批模式"**——所有工具调用必须全部完成才进入下一轮 LLM；**dsh 是"流模式"**——每条 `tool/call` 一到达就并发启动，达到 barrier 时已经积累多个 tool/result 给下一轮。

---

## 2. E.V 当前工具调用的 12 个延迟瓶颈

按 P50 / P99 实际影响排序，每条附 `[dsh 对应设计]`。

### 🔴 P0：阻塞式批执行

**位置**：`ev/llm/tools/executor.py:81` `await asyncio.gather(*(_run(tc) for tc in tool_calls))`

**问题**：3 个工具最慢那个的延迟 = 整轮延迟。MCP 远程调用 + 联网搜索这类 2-5s 的工具一旦在 batch 里，整轮对话 TTFT 延迟被它拖死。

**dsh 对应**：`ToolExecutionMode`（`docs/subsystems/tools.md:248`），工具声明 `isConcurrencySafe: true` 才进 parallel group；声明 `exclusive` 的会形成 ordering barrier——但 barrier 后的 model request 不等齐，可以开始 streaming。

**改造**：拆 `gather` 为 per-tool Task。LLM stream 期间就启动 tool task（见 P1）。

### 🔴 P0：流式不等齐就发下一轮

**位置**：`ev/llm/brain/chat/inner_loop.py:243` `await bg_task` 等 LLM stream 完整结束

**问题**：LLM 完整回复才进入工具调度。对思考模式（DeepSeek）模型：1-2s 思考 + 1s 首字 + 3s 流式期间就能出 `tool_calls` 增量。E.V 等到 5s 后才启动工具。

**dsh 对应**：`agent-lifecycle.md` 时序图里，`assistant/chunk*` 期间 Driver 就开始预分类 pending call（"barriers and bounded rolling pool, reclassify before start"），不是等 `assistant/message` 才动作。

**改造**：在 `_run_stream_drainer` 解析到 `tool_calls` 增量时立即 spawn 工具 Task，不等 stream 收尾。

### 🔴 P0：每个工具失败同步 sleep 1s 后重试

**位置**：`ev/llm/tools/executor.py:79` `await asyncio.sleep(1.0)` 同步 sleep 阻塞 gather

**问题**：A 工具失败 → 1s sleep → 重试 A。B 工具本来能 200ms 完成，硬被 A 的 sleep 拖到 1.2s。整个 gather 退化成 max(times) + sum(retry_sleeps)。

**dsh 对应**：`tools/execute` waterfall 包裹 timeout / retry / metrics，per-tool 独立失败处理不互锁。

**改造**：retry 改 `asyncio.create_task` 单独跑，gather 只 await ready ones。

### 🟠 P1：`_get_tools()` 每轮全量重建

**位置**：`ev/llm/brain/chat/inner_loop.py:88` `tools = self._get_tools()`

**问题**：每轮调 `get_merged_tools(mcp)` → 遍历 MCP + 插件注册表 + 14 个本地工具 + 9 个开关判断。工具 schema 是稳定内容（除非 MCP 服务器增删工具或插件热重载），重复工作。

**dsh 对应**：`ToolRegistry.schemas()` 在 build request 时才构造，但通过 scope 实现 memo + invalidation。

**改造**：加 `tools_schema_generation` 计数，schema 在 (mcp_tools_hash, plugin_count, tool_flags) 不变时复用。`toolset` 切换 / 插件 reload 时 bump。

### 🟠 P1：工具超时缺失

**位置**：`ev/llm/tools/executor.py:42` `call_tool` 没有任何 timeout 包裹

**问题**：MCP `bing_search` 卡住 30s → 整轮对话卡 30s。免费档 API 限流时偶尔 hang。

**dsh 对应**：`ToolDefinition.timeoutMs`（`docs/subsystems/tools.md:73`）声明式超时 + `tools/execute` wrapper 强制 deadline。

**改造**：注册表加 `timeout` 字段（来自 dsh 的 `ToolRunContext.signal` 思路），用 `asyncio.wait_for(call, timeout=...)`；超时降级为 `{"error": "timeout"}` 不中断 gather。

### 🟠 P1：单轮结果熔断太晚

**位置**：`ev/llm/tools/executor.py:43-55` 累计 `_MAX_ROUND_TOOL_CHARS=32000` 才截断

**问题**：先让 5 个工具各跑完才发现超限，截断最后 1 个。前 4 个的 token 浪费已经在 LLM context 里。

**dsh 对应**：`ctx.tools.execute()` 在 pre-execute 阶段就能 deny / replace，超限在调度前就拒绝。

**改造**：pre-execute 阶段做 budget 检查（按 token 估算 or 字符数硬限），超 budget 的工具直接返回 stub 结果，模型依然知道"有工具被熔断"。

### 🟠 P1：工具结果强制进 LLM context 不可延迟

**位置**：`ev/llm/brain/chat/inner_loop.py:412` `messages.extend(tool_messages)`

**问题**：所有 tool 消息立即进下一轮 messages。即使用户已经接着说新话、tool 结果无关紧要，context 也被占了。

**dsh 对应**：`ToolRunContext.deferContext()`（`docs/subsystems/tools.md:191`）让工具自己决定 context 何时进。Composite 工具能把 nested dispatch context 串起来。

**改造**：tool 返回值加 `defer_to_next_turn: bool` / `concludes_turn: bool`（对标 dsh 的 `concludesTurn`），允许工具在 LLM 看来"我执行完了，可以直接给用户回复了"。

### 🟡 P2：模型路由重试不并行

**位置**：`ev/llm/brain/chat/stream_drainer.py:91` 路由服务失败 → 回退默认服务 → 再发一次

**问题**：首次调用 → 失败 → 第二次调用（用默认服务）。**两次串行**，没有 `asyncio.gather` 抢答。

**dsh 对应**：`fallback_models` chain（已有类似设计，E.V `providers.py` 也有），但 dsh 在 `agent/request-error` waterfall 上让 listener 决定 retry / preserve，**listener 可并行**。

**改造**：UCB 选主服务时，同时把 `fallback_models` 第一项也开 `create_task`，先到的赢。

### 🟡 P2：客户端同步 SDK 阻塞事件循环

**位置**：`ev/llm/brain/chat/inner_loop.py:236` `loop.run_in_executor(None, _run_stream_drainer, ctx)`（用默认 thread pool）

**问题**：用 `run_in_executor(None, ...)` 走默认 thread pool。所有 SDK 调用 + 工具调用 + 后台任务 + Web 服务全挤同一池。10 路并发会争抢。

**dsh 对应**：Node 单线程模型天然无此问题，但 E.V 是 Python → 必须用独立 `ThreadPoolExecutor` 隔离 LLM / 工具 / 推理三池。

**改造**：创建 3 个 `ThreadPoolExecutor`：`llm_pool`（4 线程）、`tool_io_pool`（8 线程）、`cpu_pool`（CPU 密集），按类型路由。

### 🟡 P2：MCP 启动延迟

**位置**：`ev/mcp/`（间接相关，每次启动 `Application` 跑 MCP 子进程）

**问题**：MCP 子进程启动是冷启动。首轮对话 2-3s 包含 MCP 启动 + 工具列表拉取。

**dsh 对应**：`MCP` 抽象在 dsh 里也是冷启动（不是它解决），但 dsh 的 `setup` callback（`core.md:68` AgentSetup）允许在 agent 创建阶段完成 MCP warmup，不在第一轮用户消息时启动。

**改造**：在 `LLMBrain.warmup()` 之外加 `McpManager.warmup()`：启动后立即拉 tool 列表缓存到 `self._mcp_tool_cache`，不阻塞首轮。

### 🟢 P3：工具 schema 校验缺失

**位置**：`ev/llm/tools/executor.py:45` `args = json.loads(tc["function"]["arguments"] or "{}")`

**问题**：LLM 生成的参数只做 JSON parse，不校验类型 / required / enum。`get_weather(city=123)` 这种会被送进函数体报错后才转 error。

**dsh 对应**：`defineTool` 的 `validateArgs()`（`docs/cookbook/adding-a-tool.md:32`）在 execute 前完整校验。

**改造**：注册表加 schema DSL（先支持 string/number/integer/boolean/array/object + required 就够 80% 用例），validate 失败直接转 `{"error": "INVALID_ARGS", "details": ...}`。

### 🟢 P3：background job 抽象缺失

**位置**：E.V 工具是"全 blocking"语义，**没有**"长任务后台化"

**问题**：插件想做"30s 抓取网页生成报告"这种工具，必须自己起 `asyncio.create_task` 塞到 plugin storage 里，状态自己管。LLM 没法在下一轮查询"那个任务完成了吗"。

**dsh 对应**：`ctx.jobs.start({ kind, label, owner, run })`（`docs/cookbook/adding-a-tool.md:79`）—— 工具有 `run_in_background` 参数，注册成 background job，LLM 在下一轮通过 `job_*` 工具查询。

**改造**：在 `plugins/context.py` 加 `ctx.start_background_job(...)` API，job 状态入 `PluginManager._jobs` 字典，自动注册 3 个工具：`jobs_list` / `jobs_get_output` / `jobs_kill`。

---

## 3. 工具作为插件（Tools as Plugins）

当前痛点：用户问"能不能丢个新工具进 `plugins/` 就能用？"——**目前不行**。E.V 的工具必须满足 3 个条件之一才生效：

1. **本地工具**：在 `plugins/builtin/tools/__init__.py` `_LOCAL_REGISTRY` 加一行 `_LOCAL_REGISTRY["my_tool"] = _my_tool`
2. **MCP 工具**：编辑 `src/mcp/mcp_config.json`
3. **插件工具**：写 `register(ctx)` 调 `ctx.register_tool({...})` ——**这条已经支持**

第 3 条路径已经能"丢目录即用"，但 `_LOCAL_REGISTRY` 必须改 import。dsh 的统一抽象是：

```ts
export function apply(ctx: Context) {
  ctx.tools.register(defineTool({ name, description, parameters, output, execute }))
}
```

**改造**：把 `ev/agent/tool_registry.py` 升格为 `ctx.tools`，统一接口。本地工具和插件工具走同一条注册路径。`_LOCAL_REGISTRY` 改为启动时 `for tool in _LOCAL_TOOL_DEFS: ctx.tools.register(...)`，`__init__.py` 删掉硬编码映射。

**结果**：本地工具降级为"代码与 builtin 插件同包的特殊情况"，用户写新工具只需：

```
plugins/builtin/my_tool/
  metadata.json
  index.py
```

`index.py`：
```python
def register(ctx):
    ctx.tools.register(
        name="my_tool",
        description="...",
        parameters={"city": {"type": "string", "required": True}},
        execute=my_impl,           # async def my_impl(args, exec) -> str
        timeout=10.0,              # 可选
    )
```

---

## 4. 工具调用延迟优化（按 P50 / P99 收益排序）

下面给出 5 级优化，**全部不破坏现有直播链路**，每条独立可做。

### 4.1 立即可做（半天内，零破坏）

#### ✅ L1-A：retry 改为非阻塞

`ev/llm/tools/executor.py:62-79` 改造为：

```python
async def _call_with_timeout(name, args, mcp, timeout=10.0):
    """per-tool timeout + 失败 fire-and-forget 重试，不阻塞 gather。"""
    try:
        return await asyncio.wait_for(call_tool(name, args, mcp), timeout=timeout)
    except (asyncio.TimeoutError, Exception) as e:
        console.warn(f"  ↳ 「{name}」首次失败（{e}），后台重试...")
        asyncio.create_task(_retry_later(name, args, mcp))  # 不 await
        return f"[{name} 执行中，1s 后重试；当前返回占位]"
```

**预期**：gather 退化为 max(times) 而不是 max + sum(retry_sleeps)。3 工具各失败 1 次时省 2s+。

#### ✅ L1-B：MCP tool list 启动期预拉

新增 `ev/mcp/manager.py::warmup()`，`Application.__init__` 后台 task 拉 tool 列表。

**预期**：首轮对话 MCP 启动延迟 -2s（从用户视角挪到后台启动期）。

#### ✅ L1-C：客户端池独立 thread pool

`ev/llm/brain/chat/inner_loop.py:236` 改为：

```python
_LLM_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="llm")
_TOOL_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="tool")

# 替换
bg_task = loop.run_in_executor(_LLM_POOL, _run_stream_drainer, ctx)
```

**预期**：高并发下 10 路对话不再争抢默认 executor。

### 4.2 一周内做（小重构，收益最大）

#### 🔧 L2-A：流期间启动工具（去 batch 化）

重写 `_chat_stream_inner` 主循环，让 `tool_calls_acc` 在子线程里一旦非空就 spawn 工具 Task：

```python
# stream_drainer.py 内
def _on_tool_call_delta(delta_tool_calls):
    """每次 tool_calls 增量回调：积累到 1 个完整 tool_call 就启动。"""
    for tc_delta in delta_tool_calls:
        if tc_delta.id and tc_delta.id not in _started_calls:
            _started_calls.add(tc_delta.id)
            # 关键：在 LLM stream 还在跑时，工具已经开始了
            loop.call_soon_threadsafe(
                _tool_tasks.start, name=tc_delta.function.name, args={...})

# inner_loop.py 主循环
while True:
    item = await q.get()
    if item is None: break
    # ... 现有切句逻辑 ...
    if item 是 tool_call 完成事件:
        # 已经在子线程启动，main 协程只 await ready ones
        pass
# 流结束后只等尚未完成的（多数已 done）
await _tool_tasks.join()
```

**预期**：典型"1 个 LLM 思考 + 1 个联网搜索"场景，**3-5s 工具延迟与 LLM 思考重叠** → 用户视角总延迟 -2-3s。

#### 🔧 L2-B：pre-execute policy + 工具 budget

`ev/llm/tools/executor.py` 拆为：

```python
async def _execute_tool_calls(mcp, tool_calls, budget_tokens=8000):
    # 1) pre-execute：单工具 budget 估算，估算 > budget 立即 stub
    # 2) 并发启动所有允许的工具
    # 3) post-execute：per-tool 截断、context deferral
    ...
```

新增 `ev/agent/tool_policy.py` 统一管 budget。dsh 的 `tools/pre-execute` waterfall 在 E.V 翻译为：

```python
# 插件钩子扩展：on_tool_call（tool_name, args）→ Decision
VALID_HOOKS = frozenset({..., "on_tool_call"})

class ToolCallEvent:
    def __init__(self, name, args): self.name, self.args = name, args
    def deny(self, reason): self.denied = reason
    def replace_result(self, new_result): self.replaced = new_result
```

**预期**：超 budget 工具"立刻返回 stub"省 1-3s；插件可以加入 policy hook（如"敏感词不调用 bing_search"）。

#### ✅ L2-C：声明式工具 schema + 校验

`ev/agent/tool_registry.py` 注册时强制 schema：

```python
def register(self, name, schema, handler, timeout=None, background=False):
    # 编译 schema 为 validator（首次注册时编译，后续复用）
    validator = _build_validator(schema)
    self._tools[name] = ToolEntry(name, schema, handler, validator, timeout, background)
```

`define_tool` 装饰器简化写法：

```python
@define_tool(name="get_weather", timeout=5.0)
async def get_weather(args, exec):
    city = args["city"]  # 已校验过类型
    return await fetch(city, signal=exec.signal)
```

**预期**：减少 LLM 幻觉调用（schema 不匹配立刻 isError），不浪费真实 tool 启动延迟。

### 4.3 中期做（架构改造，2-3 周）

#### ✅ L3-A：ctx.tools + tools/pre-execute + tools/post-execute 三段式

对齐 dsh `docs/subsystems/tools.md` 设计：

```
LLM stream
  ↓ tool/call delta
tools/pre-execute waterfall（插件 on_tool_call 钩子）
  ↓
monotonic guard（deny-only）
  ↓
tools/execute waterfall（timeout / retry / metrics wrapper）
  ↓
tool body（async def, cooperative cancel via exec.signal）
  ↓
tools/post-execute waterfall（accept / block / replace / add context）
  ↓
output.render
  ↓
tool/result 事件
  ↓
下一轮 LLM request（不 gather，可 streaming）
```

新增 `ev/agent/tool_pipeline.py`，重构 `executor.py` 调用方式。**这是 P0 延迟优化的总开关**。

#### ✅ L3-B：背景任务 API

`plugins/context.py` 加：

```python
class PluginContext:
    def start_job(self, kind: str, run: Callable, label: str = "") -> str:
        """启动后台任务，返回 job_id；可被 jobs_get_output 工具查询。"""
        job_id = f"{self._plugin_name}-{uuid4().hex[:8]}"
        asyncio.create_task(_run_job(job_id, run))
        self._manager._jobs[job_id] = JobState(...)
        return job_id
```

插件自动注册 3 个内置工具（`toolset="jobs"`）：

```python
ctx.tools.register(name="jobs_list", ...)
ctx.tools.register(name="jobs_get_output", ...)
ctx.tools.register(name="jobs_kill", ...)
```

LLM 在 `run_in_background=true` 工具时拿到 `{"kind": "background", "jobId": "..."}`，下一轮用 `jobs_get_output` 查询。

**预期**：长任务（>10s）不再阻塞对话流。

#### 🏗️ L3-C：tools 插件化（与第 3 节合并）

`ev/agent/tool_registry.py` 升格为 `ctx.tools`，`_LOCAL_REGISTRY` 拆为 `plugins/builtin/tools/*/index.py` 各一个 `register(ctx)`。

---

## 5. 推荐落地顺序

| 阶段 | 改动 | 预期 P99 延迟收益 | 风险 |
|---|---|---|---|
| Week 1 | L1-A + L1-B + L1-C | -1-2s | 极低，可灰度 |
| Week 2 | L2-A（流期间启动工具）| **-2-3s** | 中，需重构 inner_loop |
| Week 3 | L2-B（pre-execute + budget）| -1s | 中，加 policy hook |
| Week 4 | L2-C（schema 校验） | -0.5s（减少幻觉调用）| 低 |
| Week 5-6 | L3-A 三段式管线 | 与 L2-A 协同 -1s | 中-高，需回归测试 |
| Week 7 | L3-B background job | 长任务场景 -10s+ | 中 |
| Week 8 | L3-C tools 插件化 | 0（功能/扩展性） | 高，需全部本地工具迁移 |

**总预期**：从现状 P99 ~8s（含 1 工具）降到 P99 ~3-4s。L2-A 是性价比之王（最大收益、风险可控）。

---

## 6. 关键文件索引

| 文件 | 角色 |
|---|---|
| `ev/llm/brain/chat/inner_loop.py:88` | `tools = self._get_tools()` 每轮重建 |
| `ev/llm/brain/chat/inner_loop.py:236` | `run_in_executor(None, ...)` 默认池 |
| `ev/llm/brain/chat/inner_loop.py:243` | `await bg_task` 等齐 |
| `ev/llm/brain/chat/inner_loop.py:412` | `messages.extend(tool_messages)` 立即进 context |
| `ev/llm/tools/executor.py:17` | `_execute_tool_calls` 批模式入口 |
| `ev/llm/tools/executor.py:62-79` | retry 同步 sleep 1s |
| `ev/llm/utils/constants.py:8` | `_MAX_TOOL_ITERATIONS = 30` 轮上限 |
| `ev/llm/utils/constants.py:56` | `_MAX_ROUND_TOOL_CHARS = 32000` 单轮熔断 |
| `ev/llm/brain/chat/stream_drainer.py:91` | 路由失败 → 默认服务串行重试 |
| `ev/agent/tool_registry.py:48` | `register()` 缺 timeout / background 字段 |
| `plugins/base.py:23` | 17 个钩子（缺 `on_tool_call`） |
| `plugins/builtin/tools/__init__.py:54` | `_LOCAL_REGISTRY` 硬编码映射 |

---

## 7. 附录：dsh 对应设计原文索引

- 工具管线时序：`docs/tool-execution-pipeline.md`
- ToolDefinition 全字段：`docs/subsystems/tools.md:31-77`
- 并发模式 `ToolExecutionMode`：`docs/subsystems/tools.md:248`
- timeoutMs 声明：`docs/subsystems/tools.md:73`
- pre-execute waterfall：`docs/subsystems/tools.md:159-180`
- deferContext / concludesTurn：`docs/subsystems/tools.md:191-210`
- LLM 流式期间并发工具：`docs/agent-lifecycle.md:25-40`（`barriers and bounded rolling pool`）
- background job 模式：`docs/cookbook/adding-a-tool.md:79-110`
- 工具 schema DSL：`docs/subsystems/tools.md:108-150`

---

**TL;DR**：E.V 的插件系统 4 件事已经跟 dsh 同向。**最该抄的不是哲学（一切皆插件），是具体设计：流期间并发工具、pre-execute 拒绝位、声明式 timeoutMs、deferContext、background job**。L2-A（流期间启动工具）一周内能省 2-3s P99 延迟，性价比最高。
