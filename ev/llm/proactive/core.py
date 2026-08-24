"""ProactiveEngine 类骨架：__init__ + 对外接口 + LLM 决策 + prompt 组装。

队列、去重、播报实现 → executor.py；话题策略、冷却、权重 → policies.py。
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from datetime import datetime
from typing import Optional

from ev.utils import console
from ev.kernel.output_lock import get_output_lock

from .policies import (
    _SILENT_MARKERS,
    _RECENT_TYPE_WINDOW,
    _load_topic_seeds,
    next_wake_in as _next_wake_in,
    _begin_topic,
    _pick_topic,
)
from . import executor as _executor_mod


class ProactiveEngine:
    """主动对话引擎：事件驱动心跳 → 灵感话题 → LLM 自主决策 → 后台播报。"""

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
        self._stats = {"trigger": 0, "speak": 0, "dropped": 0}

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
        self._wakeup.set()

    async def heartbeat(self) -> bool:
        """自主开口检查（转发到 executor 实现）。"""
        return await _executor_mod.heartbeat(self)

    def discard_pending(self) -> int:
        """用户输入到来时清空排队中的主动消息。"""
        return _executor_mod.discard_pending(self)

    def next_wake_in(self) -> float:
        """距下一次「自主开口机会」的秒数。"""
        return _next_wake_in(self)

    # ---------- LLM 自主决策 + prompt ----------

    async def _decide_and_generate(self, topic, force: bool = False) -> Optional[str]:
        """LLM 自主决策：想说就生成发言文本，不想说返回 None。"""
        prompt = await self._build_prompt(topic, force=force)
        if not prompt:
            return None
        history_len = len(self.brain.history)
        try:
            parts = []
            async for sentence in self.brain.chat_stream(prompt, proactive=True):
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
        """解析自主开口结果：沉默 → None；否则返回发言文本。"""
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
        return text

    async def _build_prompt(self, topic, force: bool = False) -> str:
        """组装主模型的「自主开口决策」请求：agent 优先，无 agent 回退内置。"""
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
                if force:
                    prompt += (
                        "\n\n【强制开口】直播间已安静超过 25 秒，"
                        "此刻必须开口说点什么，禁止输出 <SILENT> 或任何沉默表达。")
                return prompt
            except Exception as e:
                console.error(f"[主动] agent 构造发言请求失败，回退内置：{e}")
        topic_hint = (f"可以顺着这个灵感话题聊：{topic['concept']}"
                      if topic is not None else "也可以自己决定想聊什么")
        prompt = (
            "现在是直播间的「静默时刻」。没有任何Cue，没有任何救场需求。\n"
            "你不是在答题，你是在决定「此刻这个灵魂」想不想暴露在麦克风前。\n\n"
            "请根据你当下的人设状态，从以下三种「心流路径」中选一条执行：\n"
            "1. 如果觉得无聊或疲惫 -> 输出 <SILENT>（保持沉默不丢人，避免水词）\n"
            "2. 如果心里有句憋不住的骚话/吐槽 -> 直接说（1-3句，带点冒犯或自嘲）\n"
            "3. 如果想找存在感 -> 用「反常规逻辑」解构那个灵感锚点（哪怕硬拐都行）\n\n"
            "--- 执行红线（根据历史经验修正） ---\n"
            "- 拒绝「你好吗」「今天天气」式的水词问候。\n"
            "- 拒绝教科书式的正面说教。如果话题涉及对立，用「诗意悖论」或「科学梗」消解，别站队。\n"
            "- 如果灵感锚点太无聊，允许你极端歪曲它，制造「地狱笑话」或「荒诞跳转」。\n"
            "- 玩梗可以挑衅，但收尾必须用夸张的「自我解构」把攻击性卸掉。\n\n"
            f"{topic_hint}\n"
        )
        if force:
            prompt += """
                      现在你不需要迎合谁，只需要回答自己一个问题：
                     「此时此刻，这个站在麦克风前的我，是真的想开口，还是只是在替直播间填白？」
                      - 若想开口，说出那句「不吐不快」的话，哪怕冒犯、荒诞、自嘲都行。禁止输出 <SILENT>。"""
        else:
            prompt += "规则：只要开口，必须是「非标品」的句子。无话可说时，请虔诚地输出 <SILENT>。"
        return prompt
