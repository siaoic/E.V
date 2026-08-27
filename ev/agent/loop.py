"""ReAct 任务执行循环：Plan → Execute → Observe → Re-plan。

与 LLM 主对话链路解耦：仅显式触发（!agent 命令）时启动。
高风险操作由 Sandbox 门禁兜底（默认拒绝 shell），Agent 本身不做
交互式审批，避免干扰主播端 stdin。

流程：
1. Plan：LLM 基于任务 + 历史步骤决定下一步（调用工具 或 finish）
2. Execute：经 ToolExecutor 执行（沙箱越界/高风险 → 文本观察）
3. Observe：观察结果追加进历史
4. 预算满载时压缩早期步骤为摘要（保留最近 3 步）
5. 循环直到 finish 或达到最大步数
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from openai import RateLimitError

from ev.agent.budget import TokenBudget
from ev.agent.executor import ToolExecutor
from ev.agent.iteration_budget import IterationBudget
from ev.agent.sandbox import Sandbox
from ev.kernel.output_lock import (
    STATE_IDLE, get_output_lock, set_agent_owner, set_output_owner,
    set_global_state,
)
from ev.llm.client.factory import build_thinking_extra_body
from ev.utils import config, console
from ev.utils.deadline import DeadlineExpired, run_bounded_async


@dataclass
class AgentStep:
    plan: str
    action: dict  # {"name": ..., "arguments": {...}}
    observation: str
    timestamp: float = field(default_factory=time.time)
    # 思考模式模型（如 DeepSeek）assistant 的 reasoning_content，
    # 多轮对话必须原样回传，否则 API 400 拒绝。
    reasoning_content: str = ""


# 进度回调：async (step, max_steps, action, observation)
ProgressCallback = Callable[[int, int, dict, str], Any]

# 单步执行超时（秒）：工具调用挂起时强制中断，防止单步卡死整个任务
_STEP_TIMEOUT = 60.0

# 内置 finish 工具：让走原生 tool_calls 的模型也能显式结束任务。
# 若不提供，模型只能靠「输出纯文本」表达完成，而 ReAct system 提示又要求
# 每步产生动作，部分模型（如 GLM-4-flash）会陷入反复调用工具永不终止。
_FINISH_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "finish",
        "description": "任务已完成，输出最终结果（面向用户的完整中文回答）。"
                       "调用本工具即结束任务，之后不要再调用任何工具。",
        "parameters": {
            "type": "object",
            "properties": {
                "result": {"type": "string", "description": "最终结果内容"},
            },
            "required": ["result"],
        },
    },
}

# 委派工具：把相互独立的子任务分派给并行子 Agent 执行（对标 hermes
# "Delegates and parallelizes"）。子 Agent 共享主 Agent 的 LLM 客户端与沙箱、
# 独立 TokenBudget，且不带 delegate 工具（防无限递归）。
_DELEGATE_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "delegate",
        "description": "将任务拆分为多个相互独立的子任务，分派给子 Agent 并行执行。"
                       "适用于可并行的子工作流（如分别调研多个主题再汇总）；"
                       "子任务必须自足完整（含背景与期望产出），彼此不得有依赖。",
        "parameters": {
            "type": "object",
            "properties": {
                "tasks": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "相互独立的子任务列表（每个都是完整自足的指令）",
                },
                "backend": {
                    "type": "boolean",
                    "description": "为 true 时任务入后台持久化队列离线执行（适合长任务，"
                                   "不阻塞当前对话）；缺省 false 同步并行执行并返回汇总",
                },
            },
            "required": ["tasks"],
        },
    },
}

# 子 Agent 最大步数：委派子任务的执行步数上限（低于主 Agent 上限，
# 保证子任务收敛，避免子任务失控拖垮整体）
_SUB_MAX_STEPS = 6

# 子代理阻塞工具清单（3.8，对标 Hermes DELEGATE_BLOCKED_TOOLS）：
# 子代理不得再委派（防无限递归）、不得播放音效（输出类工具并行会干扰
# 主链路的 _OUTPUT_LOCK 互斥播报）。未来接入记忆写/发消息类工具时在此追加。
_DELEGATE_BLOCKED_TOOLS = ("delegate", "play_sound_effect")

# 技能沉淀条件：成功完成任务至少需要这么多步才值得沉淀为可复用技能
_SKILL_MIN_STEPS = 2


class ReActAgent:
    def __init__(
        self,
        *,
        llm_client: Any,
        llm_model: str,
        executor: ToolExecutor,
        sandbox: Sandbox,
        budget: TokenBudget,
        max_steps: int = 8,
        max_iterations: Optional[int] = None,
        allow_delegate: bool = True,
    ) -> None:
        self._llm = llm_client
        self._model = llm_model
        self._executor = executor
        self._sandbox = sandbox
        self._budget = budget
        self._max_steps = max_steps
        # 迭代次数预算（3.1，与 TokenBudget 正交的"轮数"维度）：
        # 未显式指定时默认沿用 max_steps，不改变默认行为
        self._iter_budget = IterationBudget(
            max_iterations if max_iterations and max_iterations > 0 else max_steps)
        # 宽限调用：预算恰好耗尽那轮允许再走一步收尾（去掉工具定义的总结调用）
        self._budget_grace_call = True
        self._history: list[AgentStep] = []
        self._progress_callback: Optional[ProgressCallback] = None
        # 委派工具：注册到执行器。子 Agent 构造时传 allow_delegate=False 且
        # executor 已剔除 delegate（双保险防无限递归）。
        if allow_delegate:
            self._executor.register("delegate", _DELEGATE_TOOL_SCHEMA["function"], self._delegate)

    def on_progress(self, callback: ProgressCallback) -> None:
        """注册进度回调（每步执行后触发，用于控制中心推送）。"""
        self._progress_callback = callback

    async def close(self) -> None:
        # 池化客户端由 ev/llm/client/pool.py 统一管理生命周期，调用方禁止
        # close 池化实例（会关掉共享连接池）。agent 用完即弃，client 留池复用。
        pass

    async def run(self, task: str) -> str:
        """主循环：占用输出互斥锁执行任务，期间用户输入被拒收（§3.6）。

        锁在任务全程持有，结束（含异常）后释放并复位全局状态；任务结果
        不受锁影响。若此刻他人正在播报，则等待对方释放锁后才开始执行。
        """
        self._history.clear()
        self._budget.reset()
        self._iter_budget.reset()
        self._budget_grace_call = True
        async with get_output_lock():
            set_agent_owner()
            try:
                result = await self._run_steps(task)
            finally:
                set_output_owner(None)
                set_global_state(STATE_IDLE)
        # 任务收尾：技能沉淀 + 记忆沉淀（失败静默，不影响任务结果与关闭）
        try:
            await self._after_run(task, result)
        except Exception as e:
            console.dim(f"[Agent] 任务沉淀失败（不影响结果）：{e}")
        return result

    # ---------- 任务沉淀（对标 hermes 的 closed learning loop） ----------

    async def _after_run(self, task: str, result: str) -> None:
        """任务成功结束后：沉淀可复用技能 + 关键结论写记忆库（均为失败静默）。"""
        await self._maybe_save_skill(task, result)
        await self._maybe_sink_memory(task, result)

    async def _maybe_save_skill(self, task: str, result: str) -> None:
        """复杂任务成功完成后，把「任务→步骤→工具序列」提炼为可复用 Skill。

        由管家模型/主模型提炼（复用 call_llm_json，fail-open）；技能名已存在
        则跳过（不覆盖人工/pin 技能）。沉淀失败不影响任务结果。
        """
        cfg = config.cfg
        if not cfg.AGENT_SKILL_CREATION:
            return
        if not result or result.startswith(("达到最大步数", "LLM 调用失败",
                                            "模型未给出有效动作", "（无输出）")):
            return  # 任务未正常完成，无沉淀价值
        if len(self._history) < _SKILL_MIN_STEPS:
            return  # 步骤太少，不值得沉淀
        # 轨迹压缩成素材：每步「动作 → 观察」精简化
        lines = []
        for i, s in enumerate(self._history, 1):
            act = f"{s.action['name']}({s.action['arguments']})"
            obs = s.observation[:150]
            lines.append(f"{i}. {act} → {obs}")
        trace = "\n".join(lines)
        from ev.llm.evolution._utils import call_llm_json
        data = await call_llm_json(
            [(self._llm, self._model, "Agent 模型")],
            [
                {"role": "system", "content": _SKILL_EXTRACT_PROMPT},
                {"role": "user", "content":
                    f"[TASK]\n{task}\n\n[TRACE]\n{trace}\n\n[RESULT]\n{result[:500]}"},
            ],
            label="Agent 技能沉淀",
            temperature=0.3,
            max_tokens=1200,
        )
        if not isinstance(data, dict):
            return
        name = (data.get("name") or "").strip()
        desc = (data.get("description") or "").strip()
        content = (data.get("content") or "").strip()
        if not (name and content):
            return
        # 去重：同名技能已存在（含 pin）则跳过，不覆盖已有沉淀
        from plugins.builtin.tools.skills import get_skill_manager
        if get_skill_manager().get(name) is not None:
            console.dim(f"[Agent] 技能 {name!r} 已存在，跳过沉淀")
            return
        from ev.llm.evolution.provenance import background_review_context
        from ev.llm.evolution.skills import SkillEvolution
        with background_review_context():  # Agent 沉淀属于后台路径 → created_by=agent
            await SkillEvolution().save_skill({
                "name": name, "description": desc or "Agent 任务沉淀技能",
                "content": content,
            })

    async def _maybe_sink_memory(self, task: str, result: str) -> None:
        """把任务结论/关键事实写入记忆库，供后续对话检索召回（失败静默）。"""
        if not config.cfg.AGENT_MEMORY_SINK:
            return
        if not result or result.startswith(("达到最大步数", "LLM 调用失败")):
            return
        from tools.memory import memory
        mm = memory.get_manager()
        await mm.commit_recall_files([{
            "name": "Agent任务记录",
            "description": "Agent 任务执行结论",
            "content": f"任务：{task}\n\n结论：{result[:800]}",
            "user": mm.self_user_id,
        }])
        console.dim("[Agent] 任务结论已写入记忆库")

    # ---------- 子 Agent 委派（对标 hermes "Delegates and parallelizes"） ----------

    async def _delegate(self, tasks: list, backend: bool = False) -> str:
        """把相互独立的子任务分派给子 Agent 执行并汇总结果。

        子 Agent 共享主 Agent 的 LLM 客户端与沙箱、独立 TokenBudget，
        且 executor 不含 delegate 工具（天然防无限递归）；子任务不参与沉淀。
        backend=True 时（3.8 后台委派）：任务入 SQLite 持久化队列由常驻后台
        worker 离线执行，不阻塞当前对话；开关关闭时回退同步并行（行为不变）。
        """
        if not isinstance(tasks, list) or not tasks or not all(
                isinstance(t, str) and t.strip() for t in tasks):
            return "参数错误：tasks 必须是包含 1 条及以上子任务的数组"
        # 后台委派（3.8）：backend=True 且 AGENT_DELEGATE_BACKEND 开启
        if backend:
            from ev.agent.async_delegation import get_delegation_queue
            job_ids = []
            for t in tasks:
                job_id = get_delegation_queue().enqueue(t)
                if job_id is not None:
                    job_ids.append(job_id)
            if job_ids:
                console.ok(
                    f"[Agent] 已后台入队 {len(job_ids)} 个子任务：{job_ids}")
                return (f"已后台入队 {len(job_ids)} 个子任务（ID: {job_ids}），"
                        "由后台 worker 离线执行，完成结果落 delegation.db。")
            # 开关关闭：回退同步并行委派（现状行为）
            console.dim("[Agent] 后台委派未启用，回退同步并行执行")
        console.dim(f"[Agent] 委派 {len(tasks)} 个子任务并行执行...")

        async def _run_one(subtask: str) -> str:
            sub = ReActAgent(
                llm_client=self._llm,
                llm_model=self._model,
                executor=self._executor.without(*_DELEGATE_BLOCKED_TOOLS),
                sandbox=self._sandbox,
                budget=TokenBudget(max_tokens=self._budget.max_tokens,
                                   model_name=self._model),
                max_steps=min(self._max_steps, _SUB_MAX_STEPS),
                allow_delegate=False,  # 子任务不得再委派（防无限递归）
            )
            try:
                # 不调用 run()：避免重复竞争输出锁（主 Agent 已持有）且不沉淀
                return await sub._run_steps(subtask)
            except Exception as e:
                return f"子任务执行异常：{type(e).__name__}: {e}"

        results = await asyncio.gather(*[_run_one(t) for t in tasks])
        parts = []
        for i, (t, r) in enumerate(zip(tasks, results), 1):
            parts.append(f"子任务 {i}：「{t.strip()}」\n结果：{r[:2000]}")
        return "\n\n".join(parts)

    async def _run_steps(self, task: str) -> str:
        """ReAct 主循环（已在输出锁内执行，不含锁管理）。"""
        self._task = task
        for step in range(self._max_steps):
            # 迭代预算（3.1）：常规轮消耗一次额度
            if not self._iter_budget.consume():
                if self._budget_grace_call:
                    # 宽限收尾：预算耗尽但宽限未用 → 去掉工具定义做一次总结调用，
                    # 让模型产出最终结果而不是硬断
                    self._budget_grace_call = False
                    plan = await self._plan(summarize=True)
                    # 总结调用无工具定义：模型纯文本输出被 _plan 归一为 finish dict
                    return plan.get("result") or plan.get("reasoning") or "（无输出）"
                break
            plan = await self._plan()
            if plan["action"] == "finish":
                return plan["result"]
            tool_call = plan.get("tool_call")
            if not tool_call or not tool_call.get("name"):
                # 模型输出结构幻觉（无合法工具调用）→ 兜底结束，不抛 KeyError
                return f"模型未给出有效动作，任务未完成。最后输出：{plan.get('reasoning', '')}"
            name, args = tool_call["name"], tool_call.get("arguments", {})
            try:
                # 单步硬超时（3.17）：deadline 原语由 daemon Timer 驱动，
                # 事件循环被同步 IO 阻塞时超时仍有效
                bounded = await run_bounded_async(
                    self._executor.execute(name, args),
                    _STEP_TIMEOUT, label=f"agent:{name}")
                observation = bounded.raise_if_timed_out()
            except DeadlineExpired:
                observation = f"[TIMEOUT] 步骤超时（>{int(_STEP_TIMEOUT)}s）"
            self._history.append(AgentStep(
                plan=plan["reasoning"], action=plan["tool_call"], observation=observation,
                reasoning_content=plan.get("reasoning_content", ""),
            ))
            if self._budget.is_full(self._estimate_tokens()):
                self._compress_history()
            if self._progress_callback is not None:
                result = self._progress_callback(step + 1, self._max_steps, plan["tool_call"], observation)
                if asyncio.iscoroutine(result):
                    await result
        last = self._history[-1].observation[:200] if self._history else ""
        return f"达到最大步数（{self._max_steps}），任务未完成。最后观察：{last}"

    # ---------- 内部 ----------

    def _build_messages(self) -> list[dict]:
        """从任务 + 步骤历史重建对话消息（标准 OpenAI 工具协议）。

        每步以 assistant(tool_calls) + tool(结果) 成对回传；部分模型（如
        GLM-4-flash）若工具结果不以 tool role 回传，会认为自己发起的工具
        调用从未被响应，导致无限重试同一工具。压缩产生的摘要步骤以 user
        消息插入，保留最近步骤的完整 tool 消息。
        """
        messages = [
            {"role": "system", "content": _REACT_SYSTEM_PROMPT.format(
                workspace=str(self._sandbox.root))},
            {"role": "user", "content": f"任务：{self._task}\n\n"
                                        "请逐步完成任务。每次只调用一个工具；"
                                        "任务完成时调用 finish 工具或直接输出最终结果。"},
        ]
        for i, step in enumerate(self._history):
            if step.action["name"] == "summary":
                messages.append({"role": "user", "content": step.observation})
                continue
            call_id = f"call_{i}"
            assistant_msg = {
                "role": "assistant",
                "content": step.plan or None,
                "tool_calls": [{
                    "id": call_id, "type": "function",
                    "function": {"name": step.action["name"],
                                 "arguments": json.dumps(
                                     step.action["arguments"], ensure_ascii=False)},
                }],
            }
            # 思考模式模型（DeepSeek 等）：reasoning_content 必须原样回传，
            # 缺失会触发 400（The reasoning_content ... must be passed back）。
            if step.reasoning_content:
                assistant_msg["reasoning_content"] = step.reasoning_content
            messages.append(assistant_msg)
            messages.append({"role": "tool", "tool_call_id": call_id,
                             "content": step.observation})
        return messages

    async def _plan(self, summarize: bool = False) -> dict:
        """调 LLM 决定下一步动作：{"action": "tool"|"finish", ...}。

        summarize=True（预算耗尽宽限收尾）：不带任何工具定义，
        仅请求模型基于已有上下文给出最终总结结果。
        """
        tools = (None if summarize else
                 ([{"type": "function", "function": t}
                  for t in self._executor.schemas]
                  + [_FINISH_TOOL_SCHEMA]))
        # 思考模式与主对话链路保持一致：显式声明 thinking，避免服务端
        # 隐式开启后偶发要求回传 reasoning_content（400）。
        extra_body = build_thinking_extra_body(bool(config.cfg.LLM_THINKING))

        async def _send(with_thinking: bool = True) -> Any:
            kwargs = dict(model=self._model, messages=self._build_messages(),
                          tools=tools, tool_choice="auto", temperature=0.3)
            if with_thinking:
                kwargs["extra_body"] = extra_body
            return await self._llm.chat.completions.create(**kwargs)

        try:
            resp = await _send()
        except Exception as e:
            # 429 是服务端限流不是 thinking 不支持，不能走降级分支
            if isinstance(e, RateLimitError) or "429" in str(e):
                return {"action": "finish", "result": f"LLM 调用失败：{e}"}
            console.warn("[Agent] LLM 服务不支持 thinking 参数，降级为普通模式")
            try:
                resp = await _send(with_thinking=False)
            except Exception as e2:
                return {"action": "finish", "result": f"LLM 调用失败：{e2}"}
        usage = getattr(resp, "usage", None)
        if usage is not None:
            self._budget.consume(getattr(usage, "total_tokens", 0) or 0)
        msg = resp.choices[0].message
        content = (msg.content or "").strip()
        # 思考模式模型（DeepSeek 等）的推理内容：多轮必须回传，否则 API 400
        reasoning_content = getattr(msg, "reasoning_content", None) or ""
        if not getattr(msg, "tool_calls", None):
            # 部分模型（如 GLM-4-flash）可能不用 tool_calls，改用文本+JSON 代码块兜底
            fallback = self._extract_tool_call(content)
            if fallback is None:
                return {"action": "finish", "result": content or "（无输出）"}
            if fallback["action"] == "finish":
                return fallback
            return {
                "action": "tool",
                "reasoning": content,
                "reasoning_content": reasoning_content,
                "tool_call": fallback["tool_call"],
            }
        tc = msg.tool_calls[0]
        name = tc.function.name
        try:
            args = json.loads(tc.function.arguments or "{}")
        except json.JSONDecodeError:
            args = {}
        if name == "finish":
            # 显式完成工具：模型调用 finish 即结束任务（原生 tool_calls 路径）
            return {"action": "finish",
                    "result": str(args.get("result") or "").strip() or "（无输出）"}
        return {
            "action": "tool",
            "reasoning": (msg.content or "").strip(),
            "reasoning_content": reasoning_content,
            "tool_call": {"name": name, "arguments": args},
        }

    @staticmethod
    def _extract_tool_call(text: str) -> Optional[dict]:
        """从模型纯文本中兜底解析工具调用 JSON（兼容不用 tool_calls 的模型）。

        支持两种形态：
        - {"name": "read_file", "arguments": {"path": "..."}}
        - {"action": "finish", "result": "最终结果"}
        解析失败返回 None（由调用方按 finish 处理）。
        """
        candidates = []
        # 优先抓 ```json ... ``` 代码块
        for block in re.findall(r"```(?:json)?\s*(.*?)```", text, re.S):
            candidates.append(block.strip())
        # 再抓最外层 { ... } 块
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            candidates.append(text[start:end + 1])
        for candidate in candidates:
            try:
                data = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if not isinstance(data, dict):
                continue
            if data.get("action") == "finish":
                return {"action": "finish", "result": str(data.get("result", ""))}
            name = data.get("name") or data.get("tool")
            if not isinstance(name, str) or not name:
                continue
            arguments = data.get("arguments", data.get("args", {}))
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except json.JSONDecodeError:
                    arguments = {}
            if not isinstance(arguments, dict):
                arguments = {}
            return {"action": "tool", "tool_call": {"name": name, "arguments": arguments}}
        return None

    def _compress_history(self) -> None:
        """预算满载时压缩早期步骤为摘要（保留最近 3 步）。"""
        if len(self._history) <= 3:
            return
        old = self._history[:-3]
        self._history = self._history[-3:]
        summary_lines = []
        for s in old:
            obs = s.observation[:100]
            # 失败前缀（ERROR/BLOCKED/TIMEOUT）在摘要中显式标注，供模型判断失败原因
            marker = "（失败）" if obs.startswith(("[ERROR]", "[BLOCKED]", "[TIMEOUT]")) else ""
            summary_lines.append(f"- {s.action['name']}(...) → {obs}{marker}")
        summary = f"[早期步骤摘要] 共 {len(old)} 步：\n" + "\n".join(summary_lines)
        self._history.insert(0, AgentStep(
            plan="早期步骤摘要", action={"name": "summary", "arguments": {}},
            observation=summary,
        ))

    def _estimate_tokens(self) -> int:
        """估算历史步骤占用的 token（与 TokenBudget 同用 2.5 字符/token 口径）。"""
        total_chars = sum(len(s.plan) + len(s.observation) for s in self._history)
        return int(total_chars / TokenBudget._CHARS_PER_TOKEN)


_REACT_SYSTEM_PROMPT = """你是一个任务执行 Agent。使用 ReAct 模式：
1. 思考（Reasoning）：分析当前状态，决定下一步动作
2. 动作（Action）：调用工具，或任务完成时返回最终结果
3. 观察（Observation）：查看工具执行结果
4. 重复 1-3 直到任务完成

工作目录：{workspace}（文件路径可相对此目录，禁止越界访问其他位置）
工具调用规则：
- 每一步必须产生一个明确的动作，禁止只输出思考文字
- 优先通过 tool_calls 调用工具（每次只调用一个）
- 任务完成时直接调用 finish 工具输出最终结果（不要再调用其它工具）
- 若无法发出 tool_calls，则输出 JSON 代码块指定动作：
  ```json
  {{"name": "read_file", "arguments": {{"path": "notes.txt"}}}}
  ```
  任务完成时输出：
  ```json
  {{"action": "finish", "result": "最终结果（完整、面向用户）"}}
  ```
- 先 read_file / list_dir 了解工作目录，再决定下一步；不要重复已失败的动作
- 简洁思考，不要写冗余的内心独白；结果里不要夹带 JSON 包装"""

# 技能提炼提示词：把成功完成的 Agent 任务轨迹提炼为可复用 Skill。
# 输出协议入 system 消息（保证 JSON 解析兼容），产出 {name, description, content}。
_SKILL_EXTRACT_PROMPT = """你是技能提炼专家。根据一个 Agent 成功完成的任务及其步骤轨迹，
提炼成一份可复用的中文技能（SKILL.md 正文），供之后遇到同类任务时直接套用。

要求：
- name：简短英文/数字/下划线（如 organize_danmaku_stats），语义化
- description：一句话描述该技能适用的任务场景（15 字以内优先）
- content：Markdown 正文，包含——
  1. 适用场景（什么任务用本技能）
  2. 操作步骤（按轨迹中的实际工具序列归纳，每步写明用什么工具、关键参数与判断要点）
  3. 关键经验（轨迹观察中踩过的坑、失败动作与修正）
  4. 完成判定（什么情况下视为任务完成）
- 只输出 JSON：{"name": "...", "description": "...", "content": "..."}"""
