"""主对话与 sub-agent 的桥：长任务自动委派，结果回流主动播报。

对标 dsh 的 subagent 桥：用户问"帮我写首诗" → 主对话立刻回复
"好，正在后台写" → 委派 sub-agent → 完成后通过 proactive 引擎
主动播报结果。主对话不卡 30s 等 LLM 跑完。

- maybe_delegate(user_text, runtime) -> Optional[int]：
  Agent 开关开启时由 LLM 自主判断是否适合委派，适合则入队/启动
  后台任务并立即给用户反馈"正在后台执行"；不适合或异常返回 None，
  调用方走原对话路径（行为 100% 不变，向后兼容）。
- _run_and_report(task, runtime) -> None：后台执行任务 → 结果写
  blackboard（供其他 Agent 召回）→ 主动播报完成（fail-open）。

兼容保证：AGENT_ENABLED=false（默认）时 maybe_delegate 直接返回 None，
主对话完全不进入委派分支，行为与现状完全一致。
"""
from __future__ import annotations

import asyncio
import json
import re
from typing import TYPE_CHECKING, Optional

from ev.utils import console
from ev.agent.blackboard import get_blackboard

if TYPE_CHECKING:
    from ev.kernel.runtime import RuntimeContext


class MainChatSubAgentBridge:
    """主对话与 sub-agent 的桥：长任务自动委派，结果回流主动播报。"""

    def __init__(self) -> None:
        self.blackboard = get_blackboard()

    async def _should_delegate(self, text: str,
                               runtime: "RuntimeContext") -> bool:
        """判定规则：Agent 开关开启 + LLM 配置有效 + AI 自主判断。

        前两个是硬性前置（任一不满足直接 False，走原对话路径，
        行为 100% 不变）；最后一个交给 LLM 判断输入是否适合委派
        后台长任务（fail-open：判断失败也走原对话路径）。
        """
        cfg = runtime.cfg
        # Agent 总开关关闭：直接放行原对话路径（默认行为，向后兼容）
        if not getattr(cfg, "AGENT_ENABLED", False):
            return False
        # Agent 配置缺失：不启动必然失败的后台任务
        if not (cfg.LLM_BASE_URL and cfg.LLM_API_KEY
                and (cfg.AGENT_MODEL or cfg.LLM_MODEL)):
            return False
        if not text or not text.strip():
            return False
        # 由 LLM 自主判断是否适合委派后台执行（替代原关键词匹配）
        return await self._ai_judge_delegate(text, runtime)

    async def _ai_judge_delegate(self, text: str,
                                 runtime: "RuntimeContext") -> bool:
        """让 LLM 自主判断输入是否适合委派后台执行。

        适合：需要多步工具调用 / 联网搜索 / 文件读写 / 代码 / 写作 /
        调研 / 分析 / 总结 / 整理等长任务。
        不适合：闲聊、打招呼、单句问答、情绪表达等短交互。
        任何异常均 fail-open 返回 False（走原对话路径）。
        """
        from ev.llm.client.factory import get_async_openai_client
        cfg = runtime.cfg
        model = cfg.AGENT_MODEL or cfg.LLM_MODEL
        try:
            client = get_async_openai_client(
                api_key=cfg.LLM_API_KEY, base_url=cfg.LLM_BASE_URL,
                timeout=15.0)
            system = (
                "你是任务分发判断器。判断用户输入是否适合交给后台 AI Agent 执行。\n"
                "适合：需要多步工具调用/联网搜索/文件读写/代码/写作/调研/分析/"
                "总结/整理等长任务。\n"
                "不适合：闲聊、打招呼、单句问答、问候、情绪表达等短交互。\n"
                "只输出一行 JSON，不要任何其他文字："
                '{"delegate": true, "reason": "简短理由"}'
                ' 或 {"delegate": false, "reason": "简短理由"}'
            )
            resp = await asyncio.wait_for(
                client.chat.completions.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": text},
                    ],
                    temperature=0.1,
                ),
                timeout=10.0,
            )
            raw = (resp.choices[0].message.content or "").strip()
            return self._parse_delegate_judge(raw)
        except Exception as e:
            console.warn(f"[SubAgent] AI 委派判断失败，回退原对话：{e}")
            return False

    @staticmethod
    def _parse_delegate_judge(raw: str) -> bool:
        """解析 AI 判断输出：容忍 ```json 代码块，取 delegate 布尔。

        解析失败时兜底用正则找 "delegate": true/false，再找不到返回 False。
        """
        text = raw.strip()
        if text.startswith("```"):
            text = text.strip("`").strip()
            if text.lower().startswith("json"):
                text = text[4:].strip()
        try:
            data = json.loads(text)
            return bool(data.get("delegate"))
        except (ValueError, TypeError):
            m = re.search(r'"delegate"\s*:\s*(true|false)', text,
                          re.IGNORECASE)
            if m:
                return m.group(1).lower() == "true"
            return False

    async def maybe_delegate(self, user_text: str,
                             runtime: "RuntimeContext") -> Optional[int]:
        """主对话前判断：是否需要委派。

        命中则启动后台任务 + 立即给用户反馈"正在后台执行"，
        返回 job_id（>=0）；未命中或异常返回 None，调用方走原对话路径。
        是否命中由 LLM 自主判断（见 _ai_judge_delegate）。
        """
        try:
            if not await self._should_delegate(user_text, runtime):
                return None
            # 优先走持久化队列（AGENT_DELEGATE_BACKEND 开启时）
            from ev.agent.async_delegation import (
                delegate_backend_enabled, get_delegation_queue,
            )
            if delegate_backend_enabled():
                job_id = get_delegation_queue().enqueue(user_text)
                if job_id is not None:
                    await self._notify_user(
                        runtime, f"好的，正在后台执行：{user_text[:30]}……")
                    console.ok(
                        f"[SubAgent] 已后台入队 #{job_id}：{user_text[:60]}")
                    return job_id
            # 回退进程内后台任务（AGENT_DELEGATE_BACKEND 关闭时）
            asyncio.create_task(self._run_and_report(user_text, runtime))
            await self._notify_user(
                runtime, f"好的，正在后台执行：{user_text[:30]}……")
            console.ok(f"[SubAgent] 已启动后台任务：{user_text[:60]}")
            return 0  # 占位 job_id（进程内路径）
        except Exception as e:
            console.warn(f"[SubAgent] maybe_delegate 异常，回退原对话：{e}")
            return None

    async def _run_and_report(self, task: str,
                              runtime: "RuntimeContext") -> None:
        """后台执行任务 → 结果写 blackboard → 主动播报（fail-open）。"""
        from ev.agent import run_task
        try:
            result = await run_task(task, mcp=runtime.mcp)
        except Exception as e:
            console.warn(f"[SubAgent] 后台任务失败：{type(e).__name__}: {e}")
            return
        # 结果写黑板（其他 Agent 可读到，省一次 LLM 信息提取）
        try:
            await self.blackboard.put(
                "delegation_result",
                {"task": task, "result": (result or "")[:2000]},
                source="main_chat_bridge",
            )
        except Exception:
            pass
        # 主动播报完成（走 proactive 队列或持锁 speak_text，自带互斥）
        try:
            await self._notify_user(
                runtime, f"任务完成了：{(result or '')[:200]}")
        except Exception as e:
            console.warn(f"[SubAgent] 结果播报失败：{e}")

    async def _notify_user(self, runtime: "RuntimeContext",
                           text: str) -> None:
        """主动向用户播报一句：优先 proactive 引擎，无则持锁 speak_text。

        复用 plugins/context.py 的 send_message 同款范式，确保走
        全局 _OUTPUT_LOCK 互斥（与正在进行的对话不冲突）。
        """
        if runtime.proactive is not None:
            runtime.proactive._enqueue(text, None)
            return
        from ev.kernel.output_lock import (
            STATE_AI_SPEAKING, STATE_IDLE, get_output_lock,
            set_global_state, set_output_owner,
        )
        from ev.llm import stream
        output_lock = get_output_lock()
        async with output_lock:
            set_output_owner("subagent")
            set_global_state(STATE_AI_SPEAKING)
            try:
                if runtime.tts is not None:
                    runtime.tts.clear_interrupt()
                await stream.speak_text(
                    text, runtime.tts, runtime.face, runtime.sub)
            finally:
                set_output_owner(None)
                set_global_state(STATE_IDLE)
