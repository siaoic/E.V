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
import time
from typing import TYPE_CHECKING, Optional

from ev.utils import console
from ev.agent.blackboard import get_blackboard

if TYPE_CHECKING:
    from ev.kernel.runtime import RuntimeContext

# 委派判决缓存：(判决, monotonic 时间)；容量上限防膨胀
_JUDGE_CACHE: dict = {}
_JUDGE_CACHE_TTL = 600.0
_JUDGE_CACHE_LIMIT = 128

# 开播报用的任务简称：剥路径/引号/JSON——「给我弹"E:\AI\...jpg"」
# 不该整串读进 TTS，播成「给我弹乐谱图片」即可
_HINT_PATH_RE = re.compile(
    r"[`\"'「『]?[A-Za-z]:[/\\][^\s`\"'」』”】，。；、)\]]*[`\"'」』]?|[`\"'「『]?/[^\s`\"'」』”】，。；、)\]]+[`\"'」』]?"
)


def _task_hint(user_text: str, max_chars: int = 24) -> str:
    """把用户任务文本压成一句可播报的短提示（路径脱敏、去引号、限长）。"""
    hint = _HINT_PATH_RE.sub("乐谱图片" if re.search(
        r"\.(jpe?g|png|webp|bmp|mid)", user_text or "", re.IGNORECASE) else "文件",
        user_text or "")
    hint = re.sub(r"[`\"'“”「」『』]", "", hint)
    hint = re.sub(r"\s+", " ", hint).strip(" ：:，,、-—…")
    if not hint:
        return "任务"
    return hint[:max_chars].rstrip() + ("……" if len(hint) > max_chars else "")


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

        判据核心是「预计耗时」而非「要不要用工具」：主对话本身带完整
        工具集，1-2 次工具调用能完成的即时任务（识谱弹奏、查天气、查
        时间、播音效、读写文件、记备忘）留在主对话更快——此前判据写成
        「需要多步工具调用即适合」，导致这类简单任务也被塞给后台 Agent
        平白多花一倍时间。只有明确要求后台、或预计超过 2 分钟的长任务
        （深度调研 / 长文写作 / 多源交叉整理）才委派。
        任何异常均 fail-open 返回 False（走原对话路径）。
        """
        # 判决缓存：同文本 10 分钟内复用判决（LLM 判定有随机性，重复输入
        # 反复判会时而委派时而不委派，行为不一致；缓存顺带省一次判决延迟）
        cache_key = re.sub(r"\s+", " ", (text or "").strip())[:200]
        now = time.monotonic()
        cached = _JUDGE_CACHE.get(cache_key)
        if cached is not None and now - cached[1] < _JUDGE_CACHE_TTL:
            return cached[0]
        from ev.llm.client.factory import (
            build_thinking_extra_body, get_async_openai_client)
        cfg = runtime.cfg
        model = cfg.AGENT_MODEL or cfg.LLM_MODEL
        try:
            client = get_async_openai_client(
                api_key=cfg.LLM_API_KEY, base_url=cfg.LLM_BASE_URL,
                timeout=15.0)
            system = (
                "你是任务分发判断器。判断用户输入是否应该交给后台 AI Agent "
                "离线执行（主对话先回复「正在后台执行」，几分钟后才出结果）。\n"
                "只有两种情况输出 delegate=true：\n"
                "1) 用户明确要求后台/稍后/慢慢执行；\n"
                "2) 预计总耗时超过 2 分钟：深度调研、多来源交叉整理、长文写作、"
                "批量文件处理等。\n"
                "以下一律 delegate=false：\n"
                "- 闲聊、问候、情绪表达、单句问答；\n"
                "- 主对话用 1-2 次工具就能完成的即时任务：识谱并弹奏一张乐谱、"
                "播放歌曲/音效、查天气/时间、搜索一个事实、读写一个文件、"
                "设个提醒、记一条备忘——这些主对话自己就有工具，委派反而更慢。\n"
                "判断依据是预计耗时秒数（est_seconds），不是「要不要用工具」。\n"
                "只输出一行 JSON，不要任何其他文字："
                '{"delegate": true, "est_seconds": 300, "reason": "简短理由"}'
                ' 或 {"delegate": false, "est_seconds": 20, "reason": "简短理由"}'
            )
            kwargs = dict(
                model=model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": text},
                ],
                temperature=0,
            )
            try:
                # 显式关思考：委派判决只需一行 JSON，思考只会拖慢判定
                resp = await asyncio.wait_for(
                    client.chat.completions.create(
                        **kwargs, extra_body=build_thinking_extra_body(False)),
                    timeout=10.0,
                )
            except Exception:
                # thinking 字段不被支持 → 降级普通模式重试
                resp = await asyncio.wait_for(
                    client.chat.completions.create(**kwargs), timeout=10.0)
            raw = (resp.choices[0].message.content or "").strip()
            verdict = self._parse_delegate_judge(raw)
            if len(_JUDGE_CACHE) >= _JUDGE_CACHE_LIMIT:
                # 简单过期淘汰：丢最早写入的一半，防长直播进程缓存膨胀
                for k in sorted(_JUDGE_CACHE, key=lambda k: _JUDGE_CACHE[k][1])[
                        :_JUDGE_CACHE_LIMIT // 2]:
                    _JUDGE_CACHE.pop(k, None)
            _JUDGE_CACHE[cache_key] = (verdict, time.monotonic())
            return verdict
        except Exception as e:
            # TimeoutError 等异常 str 为空，带上异常类型便于排查
            console.warn(f"[SubAgent] AI 委派判断失败，回退原对话："
                         f"{type(e).__name__}: {e or '（无异常消息）'}")
            return False

    @staticmethod
    def _parse_delegate_judge(raw: str, *, min_delegate_seconds: float = 90.0) -> bool:
        """解析 AI 判断输出：容忍 ```json 代码块，取 delegate 布尔。

        est_seconds（模型自估的耗时）小于 min_delegate_seconds 时强制
        False——短任务即使模型误判 true 也不委派（宁走主对话，不白等）。
        解析失败时兜底用正则找 "delegate": true/false，再找不到返回 False。
        """
        text = raw.strip()
        if text.startswith("```"):
            text = text.strip("`").strip()
            if text.lower().startswith("json"):
                text = text[4:].strip()
        try:
            data = json.loads(text)
            delegate = bool(data.get("delegate"))
            if delegate:
                est = data.get("est_seconds")
                if isinstance(est, (int, float)) and est < min_delegate_seconds:
                    return False
            return delegate
        except (ValueError, TypeError):
            m = re.search(r'"delegate"\s*:\s*(true|false)', text,
                          re.IGNORECASE)
            return bool(m) and m.group(1).lower() == "true"

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
                        runtime, f"好的，正在后台执行：{_task_hint(user_text)}")
                    console.ok(
                        f"[SubAgent] 已后台入队 #{job_id}：{user_text[:60]}")
                    return job_id
            # 回退进程内后台任务（AGENT_DELEGATE_BACKEND 关闭时）
            asyncio.create_task(self._run_and_report(user_text, runtime))
            await self._notify_user(
                runtime, f"好的，正在后台执行：{_task_hint(user_text)}")
            console.ok(f"[SubAgent] 已启动后台任务：{user_text[:60]}")
            return 0  # 占位 job_id（进程内路径）
        except Exception as e:
            console.warn(f"[SubAgent] maybe_delegate 异常，回退原对话：{e}")
            return None

    async def _run_and_report(self, task: str,
                              runtime: "RuntimeContext") -> None:
        """后台执行任务 → 结果写 blackboard → 主动播报（fail-open）。"""
        from ev.agent import run_task

        def _progress(step: int, max_steps: int, action: dict,
                      observation: str) -> None:
            """每步进展只进工具日志（dim 灰色行），不进对话/不播报。"""
            console.dim(f"[SubAgent] 步骤 {step}/{max_steps}："
                        f"{(action or {}).get('name', '?')} → "
                        f"{(observation or '')[:100]}")

        try:
            result = await run_task(task, mcp=runtime.mcp, progress_cb=_progress)
        except Exception as e:
            console.warn(f"[SubAgent] 后台任务失败：{type(e).__name__}: {e}")
            return
        # 结果写黑板（其他 Agent 可读到，省一次 LLM 信息提取）——完整结果
        # （含任务执行过程等细节）进黑板与控制台日志，供召回/排查
        try:
            await self.blackboard.put(
                "delegation_result",
                {"task": task, "result": (result or "")[:2000]},
                source="main_chat_bridge",
            )
        except Exception:
            pass
        # 主动播报（走 proactive 队列或持锁 speak_text，自带互斥）：
        # 失败样式结果话术改为「后台任务没完成」；内容用 speakable_result
        # 瘦身——只播 1 句结论，过程细节/文件路径留在工具日志与黑板
        from ev.agent.loop import is_failure_result, speakable_result
        prefix = "后台任务没完成" if is_failure_result(result) else "任务完成了"
        summary = speakable_result(result) or ("任务没完成" if is_failure_result(result) else "办好了")
        try:
            await self._notify_user(runtime, f"{prefix}：{summary}")
        except Exception as e:
            console.warn(f"[SubAgent] 结果播报失败：{e}")
        # 完整结果落工具日志（控制台可见，不进对话）：排查用
        console.dim(f"[SubAgent] 任务完整结果：{(result or '')[:500]}")

    async def _notify_user(self, runtime: "RuntimeContext",
                           text: str) -> None:
        """主动向用户播报一句（模块级 notify_user 的实例方法包装）。"""
        await notify_user(runtime, text)


async def notify_user(runtime: "RuntimeContext", text: str) -> None:
    """主动向用户播报一句：优先 proactive 引擎，无则持锁 speak_text。

    复用 plugins/context.py 的 send_message 同款范式，确保走
    全局 _OUTPUT_LOCK 互斥（与正在进行的对话不冲突）。
    供 bridge 与持久化队列 worker 完成回调（components/agent.py）共用。
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
