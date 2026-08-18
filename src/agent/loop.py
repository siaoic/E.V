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

from src.agent.budget import TokenBudget
from src.agent.executor import ToolExecutor
from src.agent.sandbox import Sandbox
from src.core.output_lock import (
    STATE_IDLE, get_output_lock, set_agent_owner, set_output_owner,
    set_global_state,
)
from src.utils import config, console


@dataclass
class AgentStep:
    plan: str
    action: dict  # {"name": ..., "arguments": {...}}
    observation: str
    timestamp: float = field(default_factory=time.time)


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
            },
            "required": ["tasks"],
        },
    },
}

# 子 Agent 最大步数：委派子任务的执行步数上限（低于主 Agent 上限，
# 保证子任务收敛，避免子任务失控拖垮整体）
_SUB_MAX_STEPS = 6

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
        allow_delegate: bool = True,
    ) -> None:
        self._llm = llm_client
        self._model = llm_model
        self._executor = executor
        self._sandbox = sandbox
        self._budget = budget
        self._max_steps = max_steps
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
        try:
            await self._llm.close()
        except Exception:
            pass

    async def run(self, task: str) -> str:
        """主循环：占用输出互斥锁执行任务，期间用户输入被拒收（§3.6）。

        锁在任务全程持有，结束（含异常）后释放并复位全局状态；任务结果
        不受锁影响。若此刻他人正在播报，则等待对方释放锁后才开始执行。
        """
        self._history.clear()
        self._budget.reset()
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
        from src.llm.evolution._utils import call_llm_json
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
        from plugins.tools.skills import get_skill_manager
        if get_skill_manager().get(name) is not None:
            console.dim(f"[Agent] 技能 {name!r} 已存在，跳过沉淀")
            return
        from src.llm.evolution.skills import SkillEvolution
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

    async def _delegate(self, tasks: list) -> str:
        """把相互独立的子任务分派给并行子 Agent 执行并汇总结果。

        子 Agent 共享主 Agent 的 LLM 客户端与沙箱、独立 TokenBudget，
        且 executor 不含 delegate 工具（天然防无限递归）；子任务不参与沉淀。
        """
        if not isinstance(tasks, list) or not tasks or not all(
                isinstance(t, str) and t.strip() for t in tasks):
            return "参数错误：tasks 必须是包含 1 条及以上子任务的数组"
        console.dim(f"[Agent] 委派 {len(tasks)} 个子任务并行执行...")

        async def _run_one(subtask: str) -> str:
            sub = ReActAgent(
                llm_client=self._llm,
                llm_model=self._model,
                executor=self._executor.without("delegate"),
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
            plan = await self._plan()
            if plan["action"] == "finish":
                return plan["result"]
            tool_call = plan.get("tool_call")
            if not tool_call or not tool_call.get("name"):
                # 模型输出结构幻觉（无合法工具调用）→ 兜底结束，不抛 KeyError
                return f"模型未给出有效动作，任务未完成。最后输出：{plan.get('reasoning', '')}"
            name, args = tool_call["name"], tool_call.get("arguments", {})
            try:
                # 单步硬超时：挂起的工具调用中断并记录 [TIMEOUT] 观察，供 LLM 调整策略
                observation = await asyncio.wait_for(
                    self._executor.execute(name, args), timeout=_STEP_TIMEOUT)
            except asyncio.TimeoutError:
                observation = f"[TIMEOUT] 步骤超时（>{int(_STEP_TIMEOUT)}s）"
            self._history.append(AgentStep(
                plan=plan["reasoning"], action=plan["tool_call"], observation=observation,
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
            messages.append({
                "role": "assistant",
                "content": step.plan or None,
                "tool_calls": [{
                    "id": call_id, "type": "function",
                    "function": {"name": step.action["name"],
                                 "arguments": json.dumps(
                                     step.action["arguments"], ensure_ascii=False)},
                }],
            })
            messages.append({"role": "tool", "tool_call_id": call_id,
                             "content": step.observation})
        return messages

    async def _plan(self) -> dict:
        """调 LLM 决定下一步动作：{"action": "tool"|"finish", ...}。"""
        try:
            resp = await self._llm.chat.completions.create(
                model=self._model,
                messages=self._build_messages(),
                tools=([{"type": "function", "function": t}
                        for t in self._executor.schemas]
                       + [_FINISH_TOOL_SCHEMA]),
                tool_choice="auto",
                temperature=0.3,
            )
        except Exception as e:
            return {"action": "finish", "result": f"LLM 调用失败：{e}"}
        usage = getattr(resp, "usage", None)
        if usage is not None:
            self._budget.consume(getattr(usage, "total_tokens", 0) or 0)
        msg = resp.choices[0].message
        content = (msg.content or "").strip()
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
