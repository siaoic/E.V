"""ProactiveEngine 类骨架：__init__ + 对外接口 + LLM 决策 + prompt 组装。

重构说明（参考 新建文件夹/EV-Anthropomorphic，Neuro-sama 风格）：
  - 心跳不再无脑问 LLM「想不想说」，而是先过 Nudge 契机引擎（nudge.py）：
    只有契机命中（冷场/未读堆积/氛围变化/太久没说/弹幕爆发）才问一次 LLM，
    由主模型自主决定开口还是 [SILENT] 拒绝（契机是建议不是命令）。
  - 新增 request_speak()：被动响应式主动发言申请（LLM/技能可主动举手），
    返回结构化结果，供未来以工具形式暴露给 agent。
  - 话题策略、冷却、权重 → policies.py；队列、去重、播报 → executor.py。
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from datetime import datetime
from typing import Optional

from ev.utils import console
from ev.kernel.output_lock import get_output_lock

from . import nudge as _nudge_mod
from .nudge import NudgeEngine, NudgeEvent
from .policies import (
    _SILENT_MARKERS,
    _RECENT_TYPE_WINDOW,
    _load_topic_seeds,
    next_wake_in as _next_wake_in,
    _begin_topic,
    _pick_topic,
)
from . import executor as _executor_mod

# 契机的"保质期"：事件现场产生的契机若未能及时消费（被忙碌门控挡住），
# 超过此时长视为过期丢弃，避免陈旧契机在几分钟后突然触发生成。
_NUDGE_TTL_SEC = 20.0


def _nudge_kwargs(cfg) -> dict:
    """从 cfg 提取契机引擎阈值参数（__init__ 与热更新共用同一份映射）。"""
    return {
        "long_silence_sec": getattr(
            cfg, "PROACTIVE_NUDGE_LONG_SILENCE_SEC", 30.0),
        "silent_too_long_sec": getattr(
            cfg, "PROACTIVE_NUDGE_SILENT_TOO_LONG_SEC", 300.0),
        "many_unread_threshold": getattr(
            cfg, "PROACTIVE_NUDGE_MANY_UNREAD", 5),
        "burst_threshold": getattr(
            cfg, "PROACTIVE_NUDGE_BURST_THRESHOLD", 10),
        "burst_window_sec": getattr(
            cfg, "PROACTIVE_NUDGE_BURST_WINDOW_SEC", 30.0),
        "nudge_cooldown_sec": getattr(
            cfg, "PROACTIVE_NUDGE_COOLDOWN_SEC", 30.0),
        "repeat_gap_sec": getattr(
            cfg, "PROACTIVE_NUDGE_REPEAT_GAP_SEC", 60.0),
    }


class ProactiveEngine:
    """主动对话引擎：事件契机驱动 → 灵感话题 → LLM 自主决策 → 后台播报。"""

    def __init__(self, brain, tts, face, sub, cfg,
                 butler=None, memory_manager=None,
                 profanity_filter=None, profanity_filter_rate: float = 0.7,
                 emotion_actor=None) -> None:
        self.cfg = cfg
        self.brain = brain
        self.tts = tts
        self.face = face
        self.sub = sub
        self.butler = butler
        self.mm = memory_manager
        self.pf = profanity_filter
        self.pf_rate = profanity_filter_rate
        self.emotion_actor = emotion_actor
        self._output_lock = get_output_lock()

        self.last_interaction = time.time()
        self.active_topic: Optional[dict] = None
        self._wakeup = asyncio.Event()
        self.speaking = False
        self._recent_categories: deque = deque(maxlen=_RECENT_TYPE_WINDOW)
        self._topic_seeds = _load_topic_seeds()
        self._topic_last_used = {}
        self._topic_stats = {}

        self._queue: asyncio.Queue = asyncio.Queue(maxsize=cfg.PROACTIVE_QUEUE_MAX)
        self._worker_task: Optional[asyncio.Task] = None
        self._speak_done = asyncio.Event()
        self._recent_prompts: deque = deque(maxlen=3)
        self._stats = {"trigger": 0, "speak": 0, "dropped": 0, "silent": 0}

        # ---- Nudge 契机引擎（全局单例，弹幕埋点与本引擎共享）----
        self._nudge_enabled = bool(getattr(cfg, "PROACTIVE_NUDGE_ENABLED", True))
        self.nudge: NudgeEngine = _nudge_mod.ensure_engine(**_nudge_kwargs(cfg))
        self._pending_nudge: Optional[NudgeEvent] = None
        if self._nudge_enabled:
            self.nudge.add_listener(self._on_nudge_fired)

    # ---------- Nudge 契机 ----------

    def apply_nudge_cfg(self) -> None:
        """!config 热更新：按最新 cfg 校准契机引擎阈值（含启停开关）。

        PROACTIVE_NUDGE_ENABLED=false → 移除监听器（契机不再触发心跳）；
        重新启用 → 恢复监听。阈值经 ensure_engine 原地校准，单例不重建
        （弹幕埋点共享的全局状态不丢失）。
        """
        enabled = bool(getattr(self.cfg, "PROACTIVE_NUDGE_ENABLED", True))
        self.nudge = _nudge_mod.ensure_engine(**_nudge_kwargs(self.cfg))
        if enabled and not self._nudge_enabled:
            self.nudge.add_listener(self._on_nudge_fired)
        elif not enabled and self._nudge_enabled:
            self.nudge.clear_listeners()
            self._pending_nudge = None
        self._nudge_enabled = enabled

    def _on_nudge_fired(self, event: NudgeEvent) -> None:
        """契机命中回调（事件现场触发）：暂存契机并唤醒主循环心跳。"""
        self._pending_nudge = event
        self._wakeup.set()

    def _take_nudge(self) -> Optional[NudgeEvent]:
        """取出待处理的契机；过期（TTL 外）则丢弃并现场重查一次。"""
        nudge, self._pending_nudge = self._pending_nudge, None
        if nudge is not None and (time.time() - nudge.ts) > _NUDGE_TTL_SEC:
            console.dim(f"[主动] 契机 {nudge.reason.value} 已过期（忙碌避让），丢弃")
            nudge = None
        if nudge is None and self._nudge_enabled:
            nudge = self.nudge.check()  # 心跳路径：覆盖纯冷场（无事件）契机
        return nudge

    def notify(self, event_type: str, payload: Optional[dict] = None) -> None:
        """向契机引擎埋事件（danmaku / user_input / ai_spoke / state_change）。

        弹幕 client 等外部事件源请直接调 ev.llm.proactive.nudge.observe()。
        """
        if not self._nudge_enabled:
            return
        try:
            self.nudge.observe(event_type, payload)
        except Exception as e:
            console.dim(f"[主动] 契机状态更新出错（忽略）：{e}")

    def on_ai_spoke(self, interaction_ended: bool = True) -> None:
        """AI 播报完成回调：更新契机状态；互动式回复再补一个「氛围切换」契机。

        interaction_ended=True（键盘/弹幕回复完成）→ 产生 state_change 契机，
        让互动结束后的第一次心跳有一次开口机会（保留旧行为）；
        False（主动发言自身完成）→ 只更新时间戳，避免自问自答连环开口。
        """
        self.notify("ai_spoke")
        if interaction_ended:
            self.notify("state_change",
                        {"from": "interactive", "to": "idle"})

    def nudge_check(self) -> dict:
        """查询当前契机状态（调试/未来暴露为工具）：是否该说话 + 现场快照。"""
        if not self._nudge_enabled:
            return {"ok": True, "enabled": False,
                    "hint": "契机引擎未启用，心跳按旧逻辑直接询问主模型"}
        should, event = self.nudge.should_speak_now()
        result = {
            "ok": True, "enabled": True,
            "should_speak": should,
            "hint": event.prompt_hint if event is not None else "没有契机，继续潜水/正常处理",
            "state": self.nudge.get_state(),
        }
        if event is not None:
            result["reason"] = event.reason.value
            result["context"] = event.context
        return result

    def get_stats(self) -> dict:
        """运行统计：队列/播报 + 契机接受率（自进化复盘用）。"""
        stats = dict(self._stats)
        stats.update(self.nudge.get_stats())
        stats["queue_size"] = self._queue.qsize()
        stats["active_topic_id"] = (
            self.active_topic["topic_id"] if self.active_topic is not None else None)
        return stats

    # ---------- 对外接口 ----------

    def add_topic_seeds(self, new_seeds) -> None:
        """追加新话题种子（自我进化模块沉淀的话题），按 concept 去重合并。"""
        seen = {t.get("concept") for t in self._topic_seeds}
        for seed in new_seeds:
            if not seed.get("concept") or seed["concept"] in seen:
                continue
            self._topic_seeds.append(seed)
            seen.add(seed["concept"])

    def on_user_message(self) -> None:
        """用户发言：刷新交互时间，结束活跃话题记互动，立即给一次开口机会。"""
        self.last_interaction = time.time()
        if self.active_topic is not None:
            stats = self._topic_stats.setdefault(
                self.active_topic["topic_id"], {"use": 0, "engaged": 0})
            stats["engaged"] += 1
            self.active_topic = None
        self.notify("user_input")
        self._wakeup.set()

    async def heartbeat(self) -> bool:
        """契机门控的自主开口检查（转发到 executor 实现）。"""
        return await _executor_mod.heartbeat(self)

    def discard_pending(self) -> int:
        """用户输入到来时清空排队中的主动消息。"""
        return _executor_mod.discard_pending(self)

    def next_wake_in(self) -> float:
        """距下一次「自主开口机会」的秒数。"""
        return _next_wake_in(self)

    # ---------- 被动响应：request_speak ----------

    async def request_speak(self, topic_hint: str = "", reason: str = "",
                            nudge_reason: str = "") -> dict:
        """主动申请一次发言（被动响应式，参考 EV-Anthropomorphic 工具协议）。

        与心跳路径的区别：不经过契机门控（自己举手 = 自己给契机），
        但仍受忙碌/去重/队列约束；LLM 内部仍可输出 [SILENT] 拒说。

        Returns:
            {"ok": True, "text": ..., "topic": ...} 或
            {"ok": False, "reason": "busy" / "output_locked" / "silent" /
             "duplicate_or_queue_full" / "topic_unavailable"}
        """
        self._stats["trigger"] += 1
        # 1) 状态闸门
        if self.speaking or not self._queue.empty():
            return {"ok": False, "reason": "busy"}
        if self._output_lock.locked():
            return {"ok": False, "reason": "output_locked"}
        # 2) 选话题（指定 hint 优先，否则按权重挑）
        if topic_hint:
            topic = {"id": "user_hint", "concept": topic_hint,
                     "category": "user_requested", "tags": [],
                     "cooldown_minutes": 0}
        else:
            topic = _pick_topic(self)
            if topic is None:
                return {"ok": False, "reason": "topic_unavailable"}
        # 3) LLM 决策（内部仍允许 [SILENT] 拒说）
        text = await self._decide_and_generate(
            topic, force=False, mode="request",
            request_reason=reason, nudge_reason=nudge_reason)
        if not text:
            self._stats["silent"] += 1
            if nudge_reason and self._nudge_enabled:
                self.nudge.report_reject()
            return {"ok": False, "reason": "silent",
                    "topic": (topic or {}).get("concept", "")[:50]}
        # 4) 入队（去重/限流）+ 契机回报
        if not _executor_mod._enqueue(self, text, topic):
            return {"ok": False, "reason": "duplicate_or_queue_full"}
        if nudge_reason and self._nudge_enabled:
            self.nudge.report_act()
        return {"ok": True, "text": text[:200],
                "topic": (topic or {}).get("concept", "")[:50],
                "nudge_reason": nudge_reason}

    # ---------- LLM 自主决策 + prompt ----------

    async def _decide_and_generate(self, topic, force: bool = False,
                                   mode: str = "nudge",
                                   request_reason: str = "",
                                   nudge_reason: str = "",
                                   nudge: Optional[NudgeEvent] = None,
                                   ) -> Optional[str]:
        """LLM 自主决策：想说就生成发言文本，不想说（[SILENT]）返回 None。"""
        prompt = await self._build_prompt(
            topic, force=force, mode=mode, request_reason=request_reason,
            nudge_reason=nudge_reason, nudge=nudge)
        if not prompt:
            return None
        history_len = len(self.brain.history)
        try:
            parts = []
            async for item in self.brain.chat_stream(prompt, proactive=True):
                # chat_stream 新协议：只关心 final 段（决策文本），跳过 delta
                if isinstance(item, tuple) and item and item[0] == "final":
                    sentence = item[1]
                elif isinstance(item, str):
                    sentence = item
                else:
                    continue
                if sentence:
                    parts.append(sentence)
        except Exception as e:
            console.error(f"[主动] LLM 自主决策生成失败：{e}")
            return None
        decided = self._parse_decision("".join(parts).strip())
        if decided is None and len(self.brain.history) > history_len:
            del self.brain.history[history_len:]
        if decided is None and force:
            fallback = (topic or {}).get("concept", "")
            if fallback:
                console.dim(
                    f"[主动] 静默 {_executor_mod._FORCE_SPEAK_QUIET:.0f}s 兜底："
                    f"以灵感话题强制开口（{fallback}）")
                return fallback
        return decided

    @staticmethod
    def _parse_decision(text: str) -> Optional[str]:
        """解析自主开口结果：[SILENT]/<SILENT>/沉默 → None；否则返回发言文本。

        兼容 EV-Anthropomorphic 标记协议：[SILENT] 静默、[END] 主动收尾
        （前置文本正常播报）。
        """
        if not text:
            return None
        # [END]/<END> 收尾标记：剥掉后正常播报前置文本
        for end_tag in ("[END]", "<END>"):
            if end_tag in text:
                text = text.replace(end_tag, "").strip()
        if not text:
            return None
        t = text.strip()
        for ch in "\"'“”‘’「」『』【】()（）<>＜＞":
            t = t.replace(ch, "")
        t = t.strip(" .。!！?？~～…")
        if not t:
            return None
        lower = t.lower().replace(" ", "")
        if lower in {m.lower().replace(" ", "") for m in _SILENT_MARKERS}:
            return None
        for m in _SILENT_MARKERS:
            if t.startswith(m) and len(t) <= 12:
                return None
        # [SILENT] / <SILENT> 显式静默标记（含前后缀文本时整体视为拒说）
        upper = text.upper()
        if "[SILENT]" in upper or "<SILENT>" in upper:
            return None
        return text

    async def _build_prompt(self, topic, force: bool = False,
                            mode: str = "nudge",
                            request_reason: str = "",
                            nudge_reason: str = "",
                            nudge: Optional[NudgeEvent] = None) -> str:
        """组装「自主开口决策」请求：agent 优先，无 agent 回退内置。"""
        memory_context = ""
        if self.mm is not None:
            try:
                memory_context = await self.mm.get_memory_prompt(query="", top_k=4)
            except Exception:
                memory_context = ""
        hour = datetime.now().hour
        if self.butler is not None:
            try:
                prompt = self.butler.build_proactive_prompt(
                    topic["concept"] if topic is not None else "",
                    memory_context, hour)
                prompt += self._context_block(
                    force=force, mode=mode, request_reason=request_reason,
                    nudge_reason=nudge_reason, nudge=nudge)
                return prompt
            except Exception as e:
                console.error(f"[主动] agent 构造发言请求失败，回退内置：{e}")
        topic_hint = (f"可以顺着这个灵感话题聊：{topic['concept']}"
                      if topic is not None else "也可以自己决定想聊什么")
        prompt = (
            "[静默时刻] 没人说话，由你决定开不开口：\n"
            "1. 累/无聊 -> <SILENT>\n"
            "2. 想吐槽/骚话 -> 直接说 1~3 句（自嘲/冒犯随意）\n"
            "3. 找存在感 -> 用反常规逻辑解构灵感锚点\n"
            "红线：禁水词问候；禁说教；玩梗挑衅后用自我解构收尾。\n"
            f"{topic_hint}\n"
        )
        prompt += self._context_block(
            force=force, mode=mode, request_reason=request_reason,
            nudge_reason=nudge_reason, nudge=nudge)
        return prompt

    @staticmethod
    def _context_block(force: bool, mode: str, request_reason: str,
                       nudge_reason: str, nudge: Optional[NudgeEvent]) -> str:
        """拼装契机上下文段：本次开口机会从哪来 + 强制开口声明。"""
        lines = []
        if mode == "request":
            lines.append("\n[主动申请] 你的场，说不说由你")
            if request_reason:
                lines.append(f"理由：{request_reason}")
        else:
            kind = nudge.reason.value if nudge else nudge_reason
            hint = nudge.prompt_hint if nudge else ""
            if kind:
                lines.append(f"[契机] {kind} {hint}".rstrip())
            lines.append("说与不说仍由你决定；无话可说 -> <SILENT>")
        if force:
            lines.append("[强制] 静默 >25s，必须开口，禁止 <SILENT>")
        return "\n".join(lines) + "\n"
