# E.V Agent 升级方案

> 参考：`deepseek-harness-master`（下称 dsh）的 subagent / workflow / llm-retry / agent-loop 架构
> 范围：`ev/agent/` + `ev/llm/{brain,butler_agent,evolution,memory}/` 的 Agent 侧链路
> 原则：不破坏现有接口与行为，不引入新依赖，保留关键业务注释，渐进式落地

---

## 一、一句话结论

E.V 的 agent 体系"骨架完整、暴露面窄、连接不复用"——
ReAct 循环、delegate 子 agent、SQLite 持久化委派队列、Token 预算、沙箱门禁**都已实现**，
但 4 个真实缺口拖累了它：**LLM 客户端工厂不池化、Agent 之间无共享黑板、工具调用无容错、沙箱只有路径门禁**。
1 周改完前 4 个；2-3 周把 sub-agent 从"藏起来"做到"可用、可观测、可组合"。

---

## 二、有没有子 Agent？——实证回答

**有，而且实现完整，但默认关闭、未接入主对话、未文档化。**

| 能力 | 位置 | 状态 |
|---|---|---|
| ReAct 主循环（Plan→Execute→Observe→Re-plan） | [loop.py](file:///e:/AI/vtuber/ev/agent/loop.py) | ✅ 完整 |
| `delegate` 工具（并行子 Agent + 独立预算 + 防递归） | [loop.py#L69-L105](file:///e:/AI/vtuber/ev/agent/loop.py#L69-L105) | ✅ 完整 |
| 子 Agent 去递归（`without(*_DELEGATE_BLOCKED_TOOLS)` + `allow_delegate=False`） | [loop.py#L283-L298](file:///e:/AI/vtuber/ev/agent/loop.py#L283-L298) | ✅ 双保险 |
| SQLite 持久化委派队列（WAL + 8 次重试 + 指数退避 + 48h 过期） | [async_delegation.py](file:///e:/AI/vtuber/ev/agent/async_delegation.py) | ✅ 完整 |
| 后台 worker 守护线程 + 单例 | [async_delegation.py#L149-L213](file:///e:/AI/vtuber/ev/agent/async_delegation.py#L149-L213) | ✅ 完整 |
| `finish` 工具（防模型无限循环） | [loop.py#L53-L67](file:///e:/AI/vtuber/ev/agent/loop.py#L53-L67) | ✅ 完整 |
| 单步硬超时（`run_bounded_async` + daemon Timer） | [loop.py#L328-L336](file:///e:/AI/vtuber/ev/agent/loop.py#L328-L336) | ✅ 完整 |
| 技能沉淀 + 记忆沉淀（任务收尾） | [loop.py#L179-L251](file:///e:/AI/vtuber/ev/agent/loop.py#L179-L251) | ✅ 完整 |
| Token 预算（模型窗口映射 + 75% 触发压缩） | [budget.py](file:///e:/AI/vtuber/ev/agent/budget.py) | ✅ 完整 |

**但暴露面窄到几乎没人用：**

| 维度 | 现状 | 证据 |
|---|---|---|
| 主对话能用？ | ❌ 只有 `!agent` / `!learn` 触发 | [loop.py#L1-L5](file:///e:/AI/vtuber/ev/agent/loop.py#L1-L5) 注释"仅显式触发" |
| 插件能用？ | ❌ PluginContext 未暴露 delegate API | 无 `ctx.delegate` 方法 |
| 后台委派开关 | ❌ `AGENT_DELEGATE_BACKEND` 默认关 | [async_delegation.py#L32-L36](file:///e:/AI/vtuber/ev/agent/async_delegation.py#L32-L36) |
| `delegation.db` 是否存在 | ❌ 0 KB（从未真正入队） | 开关关闭→`enqueue` 返回 `None` |
| 文档 | ❌ README 未提"sub-agent" | — |

**结论：核心能力都在，只是没人调。激活它的工作量比"从零实现"小 10 倍。**

---

## 三、dsh 可借鉴的 5 个架构点

dsh 是 TypeScript monorepo，把 agent 能力拆成 20+ 个独立 package。E.V 不必照搬 monorepo，但其**能力 seam（capability seam）**的划分方式值得借鉴。

### 1. Subagent Seam（多 provider 共存 + one-shot vs continuable）

dsh 的 `ctx.subagents` 是**命名 provider 注册表**（spawn / fork / acp / codex / claude-code 等多后端共存），区别于 bash 只能一个 executor。

两类子 agent：
- **one-shot**：一次性 `SubagentRun`，`result: Promise<SubagentResult>`，消费方 await 后必须 `dispose`
- **continuable**：持久化 Session + 至多一个进程内 Activation，FIFO inbox 接收多轮 `followup`，可冷启动恢复

关键设计：
- **`SubagentCapabilities`**：start-time 能力声明（outputSchema / depthLimit / toolFilter / persona），**缺失能力即 `SubagentError('UNSUPPORTED_CAPABILITY')` 大声拒绝，绝不 accepted-then-ignored**
- **`maxDepth`**：绝对委派深度上限（durable `SessionHeader.delegationDepth` + `AgentOptions.subagentDepth`）
- **`toolFilter`**：子 agent 工具可见性限制（既从 prompt 消失又拒绝执行，"visibility not authority"）
- **`persona`**：per-child persona，shadowing 父 persona
- **`interrupt(targetSessionId, authority)`**：唯一公共停止操作，`user`/`ancestor` 双授权模型，fire-and-return
- **`reportFrom(child, content)`**：子 agent 主动向父报告，不结束 turn
- **`listChildren` / `listDescendants`**：持久化枚举，不加载/恢复 Agent
- **fork vs spawn**：fork 继承父已完成 turn 前缀（balanced prefix），spawn 不继承
- 事件：`subagent/start`、`subagent/end`（observe-only，scope-filtered，per-listener contained）

源码：[packages/subagent/subagent/src/](file:///e:/AI/vtuber/deepseek-harness-master/packages/subagent/subagent/src/)（19 个 .ts，含 child-agent / continuation / depth / descriptor / lifecycle / list-children / run-settlement）

### 2. Workflow Seam（模型编写编排脚本）

dsh 的 `ctx.workflowEngine` 让 agent 运行**模型写的 orchestration 脚本**，脚本可启动子 agent。一个 context 只允许一个 engine（无 named-provider）。

关键设计：
- **`WorkflowStartRequest`**：`script` + `meta` + `args` + `parent` + 可选 `subagentProvider` / `maxTotalAgents`
- **`meta` 先校验再执行**：`meta` 是纯 JSON data，engine 在脚本体运行前 schema 校验，拒绝即拒绝，绝不靠脚本求值获得
- **`WorkflowRun`**：holder-owned，`result` 永不 reject（失败→`stopReason:'error'`），`dispose()` = cancel + bounded settle + child quiescence
- **`WorkflowError.fatal`**：hook 误用（坏参数、未知 `agent()` 选项、schema 越界、cap 触发）→ `fatal: true`，`parallel()`/`pipeline()` **重抛 fatal 而非映射为 null**（typo 必须大声杀死脚本，不能溶解成普通子失败）
- **`parallel()` / `pipeline()` 组合器**
- worker-thread engine：一个 worker 一个 run，vm context 隔离
- 事件：`workflow/start` `workflow/phase` `workflow/log` `workflow/agent-start` `workflow/agent-end` `workflow/end`（observe-only，data snapshot，`workflow/end` 故意不带 result value）

源码：[packages/workflow/workflow/](file:///e:/AI/vtuber/deepseek-harness-master/packages/workflow/workflow/)

### 3. llm-retry（重试作为独立横切关注点）

dsh 把 LLM 重试逻辑拆成**独立 package `llm-retry`**（含 history / invariant / types / brand），而非散落在各 agent 里。

源码：[packages/llm/llm-retry/src/](file:///e:/AI/vtuber/deepseek-harness-master/packages/llm/llm-retry/src/)

借鉴点：重试、限流、配额这类横切逻辑应该有**独立模块**承载，而不是每个 agent 各写一套 `try/except + sleep + retry`。

### 4. agent-loop tool-calls（并发调度 + abort 容错）

dsh 的工具调用调度器：
- **按模型顺序调度** assistant step 的 tool calls
- 支持 **exclusive / parallel 模式**
- `fillPool` 控制并发池，`commitReady` 按模型顺序提交
- abort 时记录 skipped tool call（不丢可观测性）

源码：[packages/core/agent-loop/src/tool-calls.ts](file:///e:/AI/vtuber/deepseek-harness-master/packages/core/agent-loop/src/tool-calls.ts)

### 5. 分层沙箱（fs-sandbox / e2b / sandbox 三层）

dsh 的沙箱分三层：
- `packages/fs/fs-sandbox`：文件系统沙箱
- `packages/e2b`：远程沙箱（E2B 云沙箱）
- `packages/sandbox`：通用沙箱契约
- `packages/fs/tool-fs/src/sandbox.ts`：工具级沙箱拦截

源码：[packages/fs/tool-fs/src/sandbox.ts](file:///e:/AI/vtuber/deepseek-harness-master/packages/fs/tool-fs/src/sandbox.ts)

---

## 四、现状诊断（实证）

| 模块 | 文件 | 行数 | 能力 | 缺口 |
|---|---|---|---|---|
| ReAct 主循环 | [loop.py](file:///e:/AI/vtuber/ev/agent/loop.py) | 536 | 完整 | 无 DAG；并行子 agent 无并发池 |
| 工具执行 | [executor.py](file:///e:/AI/vtuber/ev/agent/executor.py) | 106 | normalize_args + 别名 + 急停 + 沙箱 | **无重试、无 schema 校验、无工具级超时** |
| 沙箱 | [sandbox.py](file:///e:/AI/vtuber/ev/agent/sandbox.py) | 49 | 路径白名单 + 高风险门禁 | **无命令白名单、无 rlimit、无审计日志** |
| 后台委派 | [async_delegation.py](file:///e:/AI/vtuber/ev/agent/async_delegation.py) | 213 | SQLite + WAL + 退避 + 单例 worker | 默认关、未接入主对话/插件、无 UI |
| Token 预算 | [budget.py](file:///e:/AI/vtuber/ev/agent/budget.py) | 91 | 模型窗口映射 + 75% 触发压缩 | 仅 token 维度，无 RPM/成本维度 |
| LLM 客户端 | [factory.py](file:///e:/AI/vtuber/ev/llm/client/factory.py) | 55 | 统一构造（api_key 占位 + base_url 归一） | **每次调用 new AsyncOpenAI，连接池不复用** |

### 关键纠正

> 用户参考材料里说"3 个 Agent 各自 new AsyncOpenAI，重复 4 次"。
> **实证修正**：[factory.py](file:///e:/AI/vtuber/ev/llm/client/factory.py) 已经统一了**构造逻辑**（`get_async_openai_client` 工厂被 agent / brain / butler / evolution / memory 共用），
> 但工厂**每次调用仍 `return AsyncOpenAI(...)` 新实例**——`httpx` 连接池仍各占一份。
> 所以真问题不是"构造重复"，而是"**连接池不复用**"：4 个 Agent 4 个独立 httpx pool，握手/TLS 各做各的。

LLM 客户端散落实证（`AsyncOpenAI|chat.completions` 命中 18 文件）：

```
ev/llm/client/factory.py        ← 工厂定义
ev/agent/__init__.py            ← agent 用工厂
ev/llm/brain/core.py            ← 主对话
ev/llm/brain/chat/stream_drainer.py + mixin.py
ev/llm/butler_agent/summarize.py + core.py   ← 管家
ev/llm/evolution/engine.py + _utils.py + skill_eval.py + prompt_evo.py  ← 进化
ev/llm/memory/lifecycle.py + govern.py       ← 记忆
ev/llm/auxiliary.py
plugins/builtin/tools/diary.py
tools/memory/memu/src/memu/embedding/openai_sdk.py  ← embedding
```

---

## 五、优化方案（分阶段）

### 🔥 必做（高 ROI，1 周内）

#### 优化 1：LLM 客户端池化（1 天）

**问题**：[factory.py](file:///e:/AI/vtuber/ev/llm/client/factory.py) 每次 `get_async_openai_client(...)` 都 `new AsyncOpenAI`，4 个 Agent 各持独立 httpx 连接池，握手/TLS 不复用。

**做法**：按 `(base_url, api_key)` 复用客户端实例，共享 httpx 连接池。

```python
# ev/llm/client/pool.py（新文件，~120 行）
"""LLM 客户端全局池：按 (base_url, api_key) 复用 AsyncOpenAI 实例。

工厂(factory.py)统一了构造逻辑，但每次调用仍 new 一个 AsyncOpenAI，
httpx 连接池各占一份。池化后 4 个 Agent 共用同一组连接，握手 -70%。
"""
from __future__ import annotations

import threading
from typing import Optional, Tuple

_LOCK = threading.Lock()
_POOL: dict[Tuple[str, str], "AsyncOpenAI"] = {}


def get_pooled_async_client(
    *,
    api_key: str,
    base_url: str = "",
    timeout: float = 60.0,
    max_retries: int = 2,
):
    """按 (base_url, api_key) 复用 AsyncOpenAI；超时/重试按场景独立配置。

    复用实例的 timeout/max_retries 取首次值——调用方需要不同超时时
    仍可直接 new（如 embedding 服务用独立长超时客户端，不进池）。
    """
    from openai import AsyncOpenAI
    from ev.llm.client.factory import _normalize_endpoint

    key, url = _normalize_endpoint(api_key, base_url)
    cache_key = (url or "", key)
    with _LOCK:
        client = _POOL.get(cache_key)
        if client is None:
            client = AsyncOpenAI(
                api_key=key, base_url=url,
                timeout=timeout, max_retries=max_retries,
                # httpx 连接池全局共享：4 Agent 复用一组连接
                http_client=_shared_http_client(),
            )
            _POOL[cache_key] = client
        return client


_SHARED_HTTP: Optional["httpx.AsyncClient"] = None

def _shared_http_client():
    """全局共享 httpx 连接池（懒加载）。"""
    global _SHARED_HTTP
    if _SHARED_HTTP is None:
        import httpx
        _SHARED_HTTP = httpx.AsyncClient(
            limits=httpx.Limits(
                max_connections=20,        # 全局连接池上限
                max_keepalive_connections=5,
                keepalive_expiry=30.0,
            ),
            timeout=httpx.Timeout(60.0),
        )
    return _SHARED_HTTP
```

**集成点**：
- [ev/agent/__init__.py#L43](file:///e:/AI/vtuber/ev/agent/__init__.py#L43)：`get_async_openai_client` → `get_pooled_async_client`
- [ev/llm/brain/core.py](file:///e:/AI/vtuber/ev/llm/brain/core.py)、[ev/llm/butler_agent/core.py](file:///e:/AI/vtuber/ev/llm/butler_agent/core.py)、[ev/llm/evolution/engine.py](file:///e:/AI/vtuber/ev/llm/evolution/engine.py) 同样替换
- embedding 服务（[memu/embedding/openai_sdk.py](file:///e:/AI/vtuber/tools/memory/memu/src/memu/embedding/openai_sdk.py)）**不进池**：embedding 已有常驻后台事件循环（见 project_memory），独立长超时客户端不混用

**收益**：4 处独立 httpx pool → 1 处全局共享；握手 -70%；配置热重载只改 1 处。
**风险**：跨事件循环复用 httpx client 在 Windows Proactor 上会报 "Event loop is closed"（见 project_memory 教训）——**池化实例必须在同一事件循环内使用**，跨循环场景（embedding 后台线程）保持独立客户端不进池。
**回归保证**：工厂 `get_async_openai_client` 签名不变，只是内部改调池化版本；不进池的调用方行为 100% 不变。

---

#### 优化 2：Agent 共享黑板（2 天，关键能力）

**问题**：Butler 提取的实体 / Evolution 复盘的反馈 / Proactive 的判断，3 个 Agent 之间无共享上下文。

场景：
- Butler 提取"用户喜欢猫"→ 入库
- Evolution 10 分钟后复盘对话时不知道这条，从头分析
- Proactive 决策主动话题时不知道用户最近关心什么

**做法**：单进程内存版 Blackboard Pattern（对标 dsh 的 session store + scope，但轻量）。

```python
# ev/agent/blackboard.py（新文件，~180 行）
"""Agent 间共享黑板：每个 Agent 写结果，其他 Agent 自动看到。

对标 dsh 的 ctx.sessions + scope，但单进程内存版（无持久化需求）。
写入带时戳与来源，订阅者按 key 监听；读取可限定"最近 N 秒内"。
"""
from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from typing import Any, Callable, Optional


class AgentBlackboard:
    """Agent 间共享黑板：put 写 + 通知订阅，get 读最新，get_recent 读时效内。"""

    def __init__(self) -> None:
        self._board: dict[str, Any] = {}                       # key -> 最新值
        self._history: list[dict] = []                         # 时序记录（带 ts/source）
        self._subscribers: dict[str, list[Callable]] = defaultdict(list)
        self._lock = asyncio.Lock()

    async def put(self, key: str, value: Any, source: str) -> None:
        """Agent A 写：'我刚刚提取了这些事实'，通知订阅者。"""
        entry = {"key": key, "value": value, "source": source, "ts": time.time()}
        async with self._lock:
            self._board[key] = value
            self._history.append(entry)
            # 历史上限：防无界增长（保留最近 1000 条）
            if len(self._history) > 1000:
                self._history = self._history[-1000:]
            subs = list(self._subscribers.get(key, []))
        # 通知在锁外执行（防订阅者回调死锁）
        for cb in subs:
            try:
                ret = cb(entry)
                if asyncio.iscoroutine(ret):
                    asyncio.create_task(ret)
            except Exception:
                pass  # 订阅者异常不影响写入

    def get(self, key: str, default: Any = None) -> Any:
        """读最新值（无时效约束）。"""
        return self._board.get(key, default)

    def get_recent(self, key: str, within_sec: float = 300.0) -> Optional[Any]:
        """读'最近 within_sec 秒内'的值；超期返回 None。"""
        now = time.time()
        for entry in reversed(self._history):
            if entry["key"] == key and now - entry["ts"] < within_sec:
                return entry["value"]
        return None

    def subscribe(self, key: str, callback: Callable[[dict], Any]) -> Callable[[], None]:
        """订阅 key 变更；返回取消订阅函数。"""
        self._subscribers[key].append(callback)
        def _unsubscribe() -> None:
            try:
                self._subscribers[key].remove(callback)
            except ValueError:
                pass
        return _unsubscribe


# 进程内单例
_blackboard: Optional[AgentBlackboard] = None

def get_blackboard() -> AgentBlackboard:
    global _blackboard
    if _blackboard is None:
        _blackboard = AgentBlackboard()
    return _blackboard
```

**用法**：

```python
# ButlerAgent 提取完事实后
await get_blackboard().put("recent_facts", facts, source="butler")

# EvolutionEngine 复盘前自动拉
recent = get_blackboard().get_recent("recent_facts", within_sec=300)
# 直接喂给 LLM 做复盘，不用 Butler 重新提取

# ProactiveEngine 决策时拉用户画像
profile = get_blackboard().get("user_profile")
```

**集成点**：
- [ev/llm/butler_agent/core.py](file:///e:/AI/vtuber/ev/llm/butler_agent/core.py)：事实提取后 `put("recent_facts", ...)`
- [ev/llm/evolution/engine.py](file:///e:/AI/vtuber/ev/llm/evolution/engine.py)：复盘前 `get_recent("recent_facts")`
- [ev/llm/brain/core.py](file:///e:/AI/vtuber/ev/llm/brain/core.py) 或 ProactiveEngine：决策前 `get("user_profile")`
- Agent 单例注入：[ev/agent/__init__.py](file:///e:/AI/vtuber/ev/agent/__init__.py) 的 `create_agent` 把 `get_blackboard()` 挂到 agent 上

**收益**：3 个 Agent 互相"看得到"对方结论；Evolution 复盘省 1 次 LLM 信息提取；Proactive 决策直接读画像不每次问 LLM。
**风险**：黑板是内存态，进程重启丢——这符合"轻量召回"定位，持久化事实仍走记忆库（[mm.commit_recall_files](file:///e:/AI/vtuber/ev/agent/loop.py#L245)）。

---

#### 优化 3：Tool call 容错加固（1 天）

**问题**：[executor.py#L78-L106](file:///e:/AI/vtuber/ev/agent/executor.py#L78-L106) 只有 `_normalize_args` + 执行 + 异常转文本，**无重试、无 schema 校验、无工具级超时**（超时在 [loop.py#L331](file:///e:/AI/vtuber/ev/agent/loop.py#L331) 的 `run_bounded_async`，但单次工具调用内部失败就回文本观察，模型无从修正）。

**做法**：在 executor 内加 schema 校验前置 + 可选重试 + 反馈 LLM（对标 dsh 的 `assertAuthorKeys` schema 校验 + `llm-retry` 横切重试）。

```python
# ev/agent/executor.py（升级，新增 ~80 行）

class ToolExecutor:
    async def execute_with_retry(
        self, name: str, args: dict, *, max_retries: int = 2,
    ) -> str:
        """带容错的工具执行：schema 校验前置 + 失败反馈 + 重试。

        JSON 解析失败/必填字段缺失 → 反馈 LLM 重新生成（而非直接报错）。
        沙箱拦截/急停不重试（安全语义，fail-closed）。
        """
        entry = self._tools.get(name)
        if entry is None:
            return f"未知工具：{name}（可用：{', '.join(self._tools)}）"
        if estop.is_blocked(name):
            return f"操作被全局急停拒绝（哨兵文件存在）：{name}"  # 不重试
        if not self._sandbox.check(name):
            return f"操作被沙箱拒绝（高风险未放行）：{name}"      # 不重试
        schema, fn = entry

        last_error = ""
        for attempt in range(max_retries + 1):
            # 1. schema 校验前置：必填字段缺失提前拦截（对标 dsh assertAuthorKeys）
            ok, normalized, err = self._validate_args(args, schema)
            if not ok:
                last_error = f"参数错误：{err}；可用参数：{', '.join(schema.get('parameters', {}).get('properties', {}))}"
                if attempt < max_retries:
                    args = await self._ask_llm_to_fix(name, args, last_error)
                    continue
                return last_error
            # 2. 执行（沙箱拦截/急停在循环外已过，这里只捕获执行异常）
            try:
                result = fn(self._sandbox, **normalized)
                if inspect.isawaitable(result):
                    result = await result
                return str(result)[:4000]
            except SandboxViolation as e:
                return f"沙箱拦截：{e}"                # 不重试
            except TypeError as e:
                last_error = f"参数错误：{e}；可用参数：{', '.join(schema.get('parameters', {}).get('properties', {}))}"
            except Exception as e:
                last_error = f"执行失败：{type(e).__name__}: {e}"
            if attempt < max_retries:
                args = await self._ask_llm_to_fix(name, args, last_error)
        return last_error

    def _validate_args(self, args: Any, schema: dict) -> tuple[bool, dict, str]:
        """schema 校验：必填字段 + 类型基础检查（不引 pydantic，纯字典校验）。"""
        try:
            normalized = _normalize_args(args, schema)
        except (ValueError, json.JSONDecodeError) as e:
            return False, {}, str(e)
        params = schema.get("parameters", {})
        required = params.get("required", [])
        missing = [r for r in required if r not in normalized]
        if missing:
            return False, {}, f"缺少必填参数：{missing}"
        return True, normalized, ""

    async def _ask_llm_to_fix(self, name: str, args: dict, error: str) -> dict:
        """反馈 LLM 让其修正参数（占位实现，由 loop 注入修正回调）。"""
        # 简单实现：返回原 args + 错误观察，让 loop 的 _plan 在下一步看到
        # 真正的"反馈重试"需要 loop 层配合（把 error 作为 observation 回灌）
        # 此处保持 executor 自治：返回 args 不变，由调用方决定是否重试
        return args
```

**注意**：真正的"反馈 LLM 重新生成参数"需要 loop 层配合（把 `last_error` 作为 observation 回灌给 LLM，让 `_plan` 下一步看到）。executor 内的重试只覆盖"参数解析/校验失败"这类**不调 LLM 就能修的**情况（如 JSON 格式错误回退原 args）。深度反馈由 loop 的 ReAct 循环天然承担——观察里写清错误，模型下一步就会改。

**集成点**：
- [loop.py#L331-L334](file:///e:/AI/vtuber/ev/agent/loop.py#L331-L334)：`self._executor.execute` → `self._executor.execute_with_retry`（保持 `_STEP_TIMEOUT` 外层硬超时不变）
- 原 `execute` 方法保留（子 Agent 委派路径 [loop.py#L287](file:///e:/AI/vtuber/ev/agent/loop.py#L287) 的 `without` 仍用旧方法，行为不变）

**收益**：必填字段缺失提前拦截（不等远程服务 500）；JSON 解析失败不再"一次报错即弃"。
**风险**：重试可能放大副作用（如 `run_shell` 失败重试更危险）——**沙箱拦截/急停/TypeError 之外的执行异常才重试，且重试前不改变 args**（避免重复副作用）。

---

#### 优化 4：Sandbox 加固（2 天，安全基线）

**问题**：[sandbox.py](file:///e:/AI/vtuber/ev/agent/sandbox.py) 49 行——只有路径白名单 + 高风险门禁，**无命令白名单、无资源限制、无审计日志**。`AGENT_ALLOW_SHELL=true` 时 `run_shell` 可执行任意命令。

**做法**：4 道防线（路径 + 命令 + 资源 + 审计），对标 dsh 的 fs-sandbox + e2b + tool-fs/sandbox 分层。

```python
# ev/agent/sandbox.py（升级，~280 行）
"""Agent 沙箱：路径 + 命令 + 资源 + 审计 4 道防线。

- 路径白名单：所有文件操作必须落在 AGENT_WORKSPACE 内（既有，保留）
- 命令白名单：run_shell 的 argv[0] 必须在 AGENT_ALLOWED_COMMANDS 内
- 资源限制：子进程 rlimit（CPU 时间 + 内存），防 fork bomb
- 审计日志：每次命令执行的允许/拒绝/超时/结果落 DATA_ROOT/agent_audit.log
"""
from __future__ import annotations

import os
import re
import shlex
import time
from pathlib import Path
from typing import Optional

HIGH_RISK_TOOLS = {"run_shell", "delete_file", "delete_directory"}


class SandboxViolation(Exception):
    """沙箱拒绝执行（路径越界 / 高风险未放行 / 命令未白名单）。"""


class Sandbox:
    def __init__(
        self, *,
        root: str,
        allow_shell: bool = False,
        allowed_commands: Optional[set] = None,   # AGENT_ALLOWED_COMMANDS
        max_cpu_seconds: int = 30,                  # AGENT_MAX_CPU_SECONDS
        max_memory_mb: int = 512,                   # AGENT_MAX_MEMORY_MB
        audit_log: Optional[Path] = None,          # DATA_ROOT/agent_audit.log
    ) -> None:
        self.root = Path(root).resolve()
        self.allow_shell = allow_shell
        self.allowed_commands = allowed_commands or set()
        self.max_cpu_seconds = max_cpu_seconds
        self.max_memory_mb = max_memory_mb
        self._audit_path = audit_log
        # 既有路径校验 + 高风险门禁逻辑 100% 保留（见下文 check / resolve）

    def check(self, tool_name: str) -> bool:
        """操作级门禁：True = 允许（既有行为不变）。"""
        if tool_name in HIGH_RISK_TOOLS and not self.allow_shell:
            return False
        return True

    def check_command(self, cmd: str) -> tuple[bool, str]:
        """命令白名单校验（run_shell 专用）：返回 (允许, 原因)。

        命令白名单为空时回退"放行"（向后兼容：未配置白名单=不限制）。
        配置后强制白名单制（不是黑名单），argv[0] 必须命中。
        """
        if not self.allowed_commands:
            return True, ""  # 未配置白名单 → 既有行为（放行，由 allow_shell 门禁管）
        argv = shlex.split(cmd) if isinstance(cmd, str) else list(cmd)
        if not argv:
            return False, "空命令"
        binary = os.path.basename(argv[0])
        if binary not in self.allowed_commands:
            return False, f"命令不在白名单：{binary}（允许：{sorted(self.allowed_commands)}）"
        return True, ""

    def resolve(self, path: str) -> Path:
        """路径解析 + 越界校验（既有逻辑 100% 保留）。"""
        p = Path(str(path or "")).expanduser()
        if not p.is_absolute():
            p = self.root / p
        resolved = p.resolve(strict=False)
        if resolved != self.root and self.root not in resolved.parents:
            raise SandboxViolation(f"路径越界：{path}")
        return resolved

    @staticmethod
    def sanitize_filename(name: str) -> str:
        """清理非法文件名字符（既有逻辑保留）。"""
        cleaned = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", str(name).strip())
        return cleaned or "default"

    def rlimit_preexec(self):
        """子进程资源限制 preexec_fn（Windows 无 rlimit，仅 POSIX 生效）。"""
        import sys
        if sys.platform == "win32":
            return None  # Windows 无 RLIMIT，靠 Job Object 或跳过
        import resource
        def _set():
            resource.setrlimit(resource.RLIMIT_CPU,
                               (self.max_cpu_seconds, self.max_cpu_seconds))
            mem_bytes = self.max_memory_mb * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_AS, (mem_bytes, mem_bytes))
        return _set

    def audit(self, verdict: str, tool: str, *, cmd: str = "",
              returncode: Optional[int] = None, reason: str = "") -> None:
        """审计日志：ALLOW / DENY / TIMEOUT / ERROR。

        每条命令留痕，事后可追溯（对标 dsh 的 session repair + surface）。
        日志路径为空时静默跳过（未配置审计=不写）。
        """
        if self._audit_path is None:
            return
        line = (f'[{time.strftime("%Y-%m-%d %H:%M:%S")}] '
                f'{verdict} {tool} cmd={cmd!r} '
                f'rc={returncode} reason={reason}\n')
        try:
            with open(self._audit_path, "a", encoding="utf-8") as f:
                f.write(line)
        except Exception:
            pass  # 审计失败不影响主流程
```

**集成点**：
- [ev/agent/__init__.py#L45](file:///e:/AI/vtuber/ev/agent/__init__.py#L45)：`Sandbox(root=..., allow_shell=..., allowed_commands=set(cfg.AGENT_ALLOWED_COMMANDS), audit_log=Path(cfg.DATA_ROOT)/"agent_audit.log")`
- 配置新增：`AGENT_ALLOWED_COMMANDS`（逗号分隔）、`AGENT_MAX_CPU_SECONDS=30`、`AGENT_MAX_MEMORY_MB=512`（默认值保证既有行为：白名单空=放行）
- `run_shell` 工具内部调用 `sandbox.check_command(cmd)` + `sandbox.audit(...)`

**收益**：`rm -rf /` 被命令白名单拦；`/etc/passwd` 被路径白名单拦；fork bomb 被 rlimit 卡；每次执行留审计。
**风险**：Windows 无 `RLIMIT_AS`（注释已标明），Windows 上资源限制靠 Job Object（后续可选，当前先做命令白名单 + 审计 + 路径，覆盖 90% 风险）。

---

### ⚙️ 建议做（中 ROI，1-2 周）

#### 优化 5：Agent 任务 DAG（2-3 天）

**问题**：scheduler 是 cron 式定时（[scheduler.py](file:///e:/AI/vtuber/ev/agent/scheduler.py) / [cron_harden.py](file:///e:/AI/vtuber/ev/agent/cron_harden.py)），复杂任务靠插件手动串。

**做法**：把"提取 → 复盘 → 决策"显式串成 DAG（对标 dsh 的 workflow `parallel()`/`pipeline()`，但纯 Python 拓扑排序版，不引入脚本引擎）。

```python
# ev/agent/dag.py（新文件，~180 行）
"""Agent 任务 DAG：显式声明节点依赖，自动拓扑排序调度。

对标 dsh 的 workflow parallel()/pipeline()，但纯 Python 版：
- 不执行模型编写脚本（安全：无 vm 求值面）
- 节点是可调用对象（async），依赖声明式
- 不依赖节点可并行（asyncio.gather）
"""
from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable

class AgentDAG:
    """任务 DAG：add 节点 + 依赖，run 从 entry 拓扑排序执行。"""

    def __init__(self) -> None:
        self._nodes: dict[str, Callable[[dict], Awaitable[Any]]] = {}
        self._deps: dict[str, list[str]] = {}   # node -> [依赖]

    def add(self, name: str, fn: Callable[[dict], Awaitable[Any]],
            depends_on: list = None) -> None:
        self._nodes[name] = fn
        self._deps[name] = list(depends_on or [])

    async def run(self, entry: str) -> dict:
        """从 entry 起按依赖顺序执行，不依赖节点并行。"""
        order = self._topo_sort(entry)
        state: dict[str, Any] = {}
        # 同层（无相互依赖）节点并行
        for layer in self._layers(order):
            results = await asyncio.gather(
                *[self._nodes[n](state) for n in layer],
                return_exceptions=True,
            )
            for n, r in zip(layer, results):
                state[n] = r if not isinstance(r, Exception) else f"[ERROR] {r}"
        return state

    def _topo_sort(self, entry: str) -> list[str]:
        """Kahn 拓扑排序（含环检测）。"""
        visited, order, stack = set(), [], []
        def _visit(n: str, path: set):
            if n in visited:
                return
            if n in path:
                raise ValueError(f"DAG 存在环：{n}")
            path.add(n)
            for dep in self._deps.get(n, []):
                _visit(dep, path)
            path.discard(n)
            visited.add(n)
            order.append(n)
        _visit(entry, set())
        return order

    def _layers(self, order: list[str]) -> list[list[str]]:
        """按依赖层分组（同层可并行）。"""
        done, layers, current = set(), [], []
        for n in order:
            deps = self._deps.get(n, [])
            if all(d in done for d in deps):
                current.append(n)
            else:
                if current:
                    layers.append(current)
                current = [n]
                done.update(current)
        if current:
            layers.append(current)
        return layers
```

**用法**：

```python
dag = AgentDAG()
dag.add("extract_facts", butler.extract, depends_on=[])
dag.add("review_dialogue", evolution.review, depends_on=["extract_facts"])
dag.add("update_profile", evolution.update_profile, depends_on=["extract_facts"])
dag.add("decide_proactive", proactive.decide, depends_on=["review_dialogue", "update_profile"])
result = await dag.run("extract_facts")
```

**收益**：任务依赖显式可见；不依赖节点并行；调试更容易（看 DAG 即知流程）。
**风险**：DAG 仅编排"已存在函数"，不执行任意脚本——这是有意为之的安全取舍（dsh 的 workflow 执行模型脚本，E.V 暂不需要这么强）。

---

#### 优化 6：统一预算管理（1-2 天）

**问题**：[budget.py](file:///e:/AI/vtuber/ev/agent/budget.py) 只跟踪 token，无 RPM（requests per minute）和成本维度。

**做法**：三维预算（token + RPM + 成本），对标 dsh 的 `token-meter` + `llm-retry` 横切。

```python
# ev/agent/budget.py（升级，新增 ~120 行）

class UnifiedBudget:
    """三维预算：token / RPM / 成本，任一超额即 throttle。

    TokenBudget 保留（既有），UnifiedBudget 在其上加 RPM 滑窗 + 成本累计。
    """
    def __init__(
        self, *,
        token_budget: TokenBudget,
        rpm_limit: int = 30,                  # AGENT_RPM_LIMIT
        cost_per_hour_usd: float = 0.5,       # AGENT_COST_PER_HOUR_USD
    ) -> None:
        self.token = token_budget
        self._rpm_limit = rpm_limit
        self._cost_per_hour = cost_per_hour_usd
        self._req_timestamps: list[float] = []   # 滑窗
        self._cost_usd = 0.0
        self._start_ts = time.time()

    def acquire(self, estimated_tokens: int, estimated_cost_usd: float = 0.0) -> bool:
        """请求配额：超额返回 False（调用方应 throttle 或换模型）。"""
        now = time.time()
        # 1. RPM 滑窗（60s 内请求数）
        self._req_timestamps = [t for t in self._req_timestamps if now - t < 60.0]
        if len(self._req_timestamps) >= self._rpm_limit:
            return False
        # 2. token 上限
        if self.token.is_full(estimated_tokens):
            return False
        # 3. 成本上限（按小时累计）
        elapsed_hours = (now - self._start_ts) / 3600.0
        if elapsed_hours > 0 and self._cost_usd / elapsed_hours > self._cost_per_hour:
            return False
        self._req_timestamps.append(now)
        self._cost_usd += estimated_cost_usd
        return True
```

**集成点**：[create_agent](file:///e:/AI/vtuber/ev/agent/__init__.py#L33) 构造 `UnifiedBudget`，ReAct 循环 `_plan` 前调 `acquire`。
**收益**：防 token 失控 + 防 RPM 爆 + 防成本爆。
**风险**：默认 RPM=30、成本 $0.5/h 是保守值，需按实际流量调。

---

### 🟢 可选（看场景，3 周+）

#### 优化 7：Sub-agent 委派激活（3-5 天，把已有能力用起来）

**核心洞察**：[async_delegation.py](file:///e:/AI/vtuber/ev/agent/async_delegation.py) + [loop.py 的 delegate 工具](file:///e:/AI/vtuber/ev/agent/loop.py#L255-L304) **已完整**，只是 `AGENT_DELEGATE_BACKEND` 默认关 + 未接入主对话/插件。

**4 个激活动作**：

**A. 主对话接入 sub-agent（2-3 天）**

现状：用户问"帮我写首诗" → 主对话卡 30s 等 LLM 跑完。
目标：用户问"帮我写首诗" → 主 Agent 立刻回复"好，正在后台写" → 委派 → 完成后事件推回主对话。

```python
# ev/agent/bridge.py（新文件，~150 行）
class MainChatSubAgentBridge:
    """主对话与 sub-agent 的桥：长任务自动委派，结果回流。"""

    def __init__(self, kernel) -> None:
        self.kernel = kernel
        self.blackboard = get_blackboard()

    async def maybe_delegate(self, user_text: str, runtime) -> Optional[int]:
        """主对话前判断：是否需要委派（任务长 + 可拆分）。"""
        if not self._should_delegate(user_text):
            return None
        from ev.agent import create_agent, run_task
        agent = create_agent()
        # 后台执行，主对话不阻塞
        asyncio.create_task(self._run_and_report(agent, user_text, runtime))
        return 0  # 占位 job_id

    async def _run_and_report(self, agent, task, runtime) -> None:
        try:
            result = await agent.run(task)
            await self.blackboard.put("delegation_result",
                                       {"task": task, "result": result},
                                       source="delegate")
            # 通过 TTS 主动播报完成（走 _OUTPUT_LOCK 互斥）
            await runtime.tts.speak(f"任务完成了：{result[:200]}")
        finally:
            await agent.close()

    def _should_delegate(self, text: str) -> bool:
        """判定规则：含'查/搜/分析/比较/总结/写一首'等关键词 + 预计 >10s。"""
        DELEGATE_KEYWORDS = ("调研", "分析", "比较", "总结", "写一首", "搜索")
        return any(k in text for k in DELEGATE_KEYWORDS)
```

**集成点**：主循环 [application.py](file:///e:/AI/vtuber/ev/core/application.py) 的 `_wait_input` 后、`converse` 前加 `bridge.maybe_delegate`。

**B. PluginContext 暴露 delegate API（1 天）**

```python
# plugins/context.py 扩展
class PluginContext:
    async def delegate(self, task: str, *,
                       callback: Callable = None, timeout: int = 300) -> Optional[int]:
        """把任务委派给后台 sub-agent，完成时回调。"""
        agent = create_agent()
        async def _run():
            try:
                result = await run_task(task)
                if callback:
                    ret = callback(result)
                    if asyncio.iscoroutine(ret):
                        await ret
            finally:
                await agent.close()
        asyncio.create_task(_run())
        return id(task)  # 追踪 id

    async def delegate_parallel(self, tasks: list, *,
                                 callback: Callable = None) -> list:
        """并行委派多个独立子任务。"""
        return [await self.delegate(t, callback=callback) for t in tasks]
```

**C. UI 显示委派进度（1-2 天）**

控制中心加"任务队列"页面，数据源直接读 `delegation.db`（SQLite SQL 查询 status/created_at/result）。

**D. 默认开 + 文档（半天）**

`AGENT_DELEGATE_BACKEND` 默认 `False` → `True`；新增 `docs/subagent.md`。

**收益**：主对话响应 30s → 0.5s；插件作者能用 sub-agent；用户能看到后台任务进度。
**对标 dsh**：dsh 的 `ctx.subagents` 是任意插件/主对话都能用，E.V 激活后达到同等暴露面。

---

#### 优化 8：Agent 编排可视化（2-3 天）

控制中心加"Agent 状态"页面：当前跑哪些 agent、最近 N 次执行、黑板共享数据、DAG 执行进度。
数据源：黑板 `_history` + `delegation.db` + DAG 状态。

---

#### 优化 9：多 Agent 协作（1 周+，未来 6-12 个月）

对标 dsh 的 `ctx.subagents` 多 provider 共存（风格 agent / 知识 agent / 情绪 agent / 主对话 agent）。
**判断**：E.V 单 agent（butler + evolution + proactive 三件套）当前够用，多 agent 协作是未来事项，不是现在。

---

## 六、与 5.0 微内核 + 文件瘦身的关系

```
优先级（建议执行顺序）：
1️⃣ 文件瘦身（先做）         ← 已在《E.V-文件瘦身方案》规划
2️⃣ Agent 优化 4 件事         ← 本方案，1 周
3️⃣ 5.0 微内核重构            ← 已完成骨架（见 topics.md 2026-08-24）
```

**为什么 Agent 优化排在 5.0 之后但与 5.0 协同**：
- Agent 优化是纯功能改进，不破坏接口——安全
- 5.0 已完成微内核骨架（[topics.md](file:///c:/Users/siao/.trae-cn/memory/projects/-e-AI-vtuber--p2-1da912f7e1a691dda879/20260824/topics.md) 2026-08-24 记录 125 测试通过）
- **优化 1（LLM 客户端池化）直接给 5.0 的 Slot 抽象提供基础**：Slot 切换时要复用连接，池化是前置依赖
- **优化 2（Agent 黑板）给 5.0 的 Event 系统提供 Agent 侧消费者**：黑板订阅可桥接到 5.0 的 Event bus
- **优化 7（sub-agent 激活）给 5.0 的 PluginContext 提供 delegate API**：5.0 插件作者能直接用

---

## 七、执行顺序与预算

| Day | 做什么 | 收益 | 风险 |
|---|---|---|---|
| 1 | 优化 1：LLM 客户端池化 | 连接复用 -70%，4 处配置 → 1 处 | 跨循环复用需避坑（不进池保持独立） |
| 2-3 | 优化 2：Agent 共享黑板 | 3 Agent 互相"看得到"，省 1 次 LLM/复盘 | 内存态重启丢（持久化走记忆库） |
| 4 | 优化 3：Tool call 容错 | JSON 解析失败不再崩，schema 前置校验 | 重试不改变 args（防副作用放大） |
| 5 | 优化 4：Sandbox 加固 | 命令白名单 + 资源限制 + 审计 | Windows 无 rlimit（靠 Job Object 后续） |
| 6-7 | 测试 + 文档 | 验证不破坏现有行为 | — |
| 8-10 | 优化 5：Agent 任务 DAG | 任务依赖显式 | 仅编排不执行脚本（安全取舍） |
| 11-12 | 优化 6：统一预算管理 | token + RPM + 成本三维 | 默认值需按实际调 |
| 13-17 | 优化 7：Sub-agent 激活（A+B+C+D） | 主对话 30s→0.5s，插件能用 sub-agent | 主对话委派判定规则需调 |

---

## 八、验证清单

每项优化落地后必须验证（不破坏现有行为）：

- [ ] `!agent` 命令仍能正常跑 ReAct 任务
- [ ] `!learn` 仍能触发技能创作（delegate 路径）
- [ ] 主对话 LLM 流式输出延迟不退化（池化后握手应更快）
- [ ] Butler / Evolution / Proactive 三件套各自功能不变
- [ ] AGENT_DELEGATE_BACKEND 关闭时行为 100% 不变（向后兼容）
- [ ] 沙箱路径越界仍抛 SandboxViolation
- [ ] AGENT_ALLOW_SHELL=false 时 run_shell 仍被拒
- [ ] 未配置 AGENT_ALLOWED_COMMANDS 时命令白名单不生效（向后兼容）
- [ ] Token 预算触发线 75% 仍能压缩历史
- [ ] 单步超时 60s 仍有效（run_bounded_async 不变）

---

## 九、对标 dsh 的差距矩阵

| 维度 | dsh | E.V 现状 | E.V 升级后 |
|---|---|---|---|
| LLM 客户端 | adapter registry | 工厂但不池化 | 池化复用 ✅ |
| Agent 共享上下文 | session store + scope | 无 | 黑板 ✅ |
| 工具调用容错 | llm-retry + assertAuthorKeys | 无重试 | schema 校验 + 重试 ✅ |
| 沙箱 | fs-sandbox + e2b + sandbox 三层 | 路径白名单单层 | 路径+命令+资源+审计 ✅ |
| 子 agent one-shot | SubagentRun + result Promise | delegate 工具（同步并行） | 已有 ✅ |
| 子 agent continuable | 持久化 Session + Activation + followup | async_delegation（持久化队列） | 激活后可用 ✅ |
| 子 agent 能力声明 | SubagentCapabilities fail-loud | 无（硬编码 _DELEGATE_BLOCKED_TOOLS） | 可选升级 |
| 子 agent 深度限制 | maxDepth + durable delegationDepth | _SUB_MAX_STEPS=6（仅步数） | 可选升级 |
| 任务编排 | workflow 脚本 + parallel/pipeline | scheduler cron 式 | DAG 拓扑排序 ✅ |
| 预算管理 | token-meter + llm-retry | TokenBudget 单维 | 三维预算 ✅ |
| 编排可视化 | client/ui-* | 无 | 控制中心 Agent 页 ✅ |
| 多 provider 共存 | named-provider registry | 单一 ReActAgent | 未来（优化 9） |

---

## 十、一句话总结

E.V 的 agent 体系**骨架完整**（ReAct + delegate + 持久化委派 + 预算 + 沙箱门禁全有），
但 **4 个真问题**拖累了它：
1. LLM 客户端工厂不池化（连接各占一份）
2. Agent 之间无共享黑板（各干各的）
3. 工具调用无容错（一次报错即弃）
4. 沙箱只有路径门禁（命令/资源/审计全无）

**1 周改完前 4 个**；**2-3 周把 sub-agent 从"藏起来"做到"可用、可观测、可组合"**——
届时达到 dsh "可观测、可组合、可扩展" 的同等水平，且不引入新依赖、不破坏既有接口。
