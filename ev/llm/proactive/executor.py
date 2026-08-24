"""主动发言引擎的队列/去重/播报 worker + heartbeat 的实现。

全部以「接受 ProactiveEngine self 作为首参数」的模块函数形式存在，
由 core.ProactiveEngine 的同名方法转发。
"""

from __future__ import annotations

import asyncio
import time
from difflib import SequenceMatcher
from typing import List, Optional

from ev.utils import console
from ev.utils.constants import ROLE_AI_ALIAS, SOURCE_PROACTIVE
from ev.llm import stream
from ev.kernel.output_lock import (
    STATE_AGENT_THINKING, STATE_AI_SPEAKING, STATE_IDLE,
    set_output_owner, set_global_state, is_idle,
    is_danmaku_pending,
)

from .policies import (
    _ACTIVE_TOPIC_TIMEOUT,
    _FORCE_SPEAK_QUIET,
    _pick_topic,
    _log_heartbeat,
    _begin_topic,
)


# ---------- heartbeat（对外核心方法）----------

async def heartbeat(self) -> bool:
    """自主开口检查：由主模型决定此刻想不想说话、想说什么。"""
    if not self.cfg.PROACTIVE_ENABLED:
        return False
    if self.active_topic is not None:
        if time.time() - self.active_topic["started_at"].timestamp() > _ACTIVE_TOPIC_TIMEOUT:
            self.active_topic = None
    if self.speaking or not self._queue.empty():
        return False
    if self.cfg.AGENT_AVOID_MAIN_LLM and (
            not is_idle() or self._output_lock.locked()):
        return False
    if is_danmaku_pending():
        return False
    if self.active_topic is not None:
        return False
    _log_heartbeat(self)
    force_speak = (time.time() - self.last_interaction) >= _FORCE_SPEAK_QUIET
    topic = _pick_topic(self)
    text = await self._decide_and_generate(topic, force=force_speak)
    if not text:
        return False
    return _enqueue(self, text, topic)


# ---------- 主动消息队列（限流 / 去重 / 丢弃）----------

def discard_pending(self) -> int:
    """用户输入到来时清空排队中的主动消息（优先响应用户）。"""
    dropped = 0
    while True:
        try:
            self._queue.get_nowait()
            dropped += 1
        except asyncio.QueueEmpty:
            break
    if dropped:
        self._stats["dropped"] += dropped
        console.dim(f"[主动] 用户输入优先，丢弃 {dropped} 条排队中的主动消息")
    return dropped


def _enqueue(self, text: str, topic: Optional[dict]) -> bool:
    """把一条主动发言放入队列；队列满丢弃最旧、与近期内容重复丢弃。"""
    kind = "topic" if topic is not None else "emotional"
    if _is_duplicate(self, text, kind):
        self._stats["dropped"] += 1
        console.dim("[主动] 与近期内容高度相似，丢弃本条主动消息")
        return False
    if self._queue.full():
        try:
            self._queue.get_nowait()
            self._stats["dropped"] += 1
            console.dim("[主动] 队列已满，丢弃最旧的主动消息")
        except asyncio.QueueEmpty:
            pass
    item = {"tag": "agent_proactive", "kind": kind,
            "text": text, "topic": topic}
    try:
        self._queue.put_nowait(item)
    except asyncio.QueueFull:
        self._stats["dropped"] += 1
        return False
    self._stats["trigger"] += 1
    _ensure_worker(self)
    return True


def _ensure_worker(self) -> None:
    """确保消费 worker 存活（异常退出后下次入队自动重建）。"""
    if self._worker_task is not None and not self._worker_task.done():
        return
    self._worker_task = asyncio.create_task(
        _worker(self), name="proactive_worker")


async def _worker(self) -> None:
    """主动消息消费循环：忙碌过滤后播报，异常捕获不退出。"""
    while True:
        item = await self._queue.get()
        try:
            if self.cfg.AGENT_AVOID_MAIN_LLM and not is_idle():
                self._stats["dropped"] += 1
                console.dim("[主动] 正在忙碌（用户/AI 处理中），丢弃本条主动消息")
                continue
            if is_danmaku_pending():
                self._stats["dropped"] += 1
                console.dim("[主动] 弹幕回复已敲定，主动避让，丢弃本条主动消息")
                continue
            await _speak_item(self, item)
            self._recent_prompts.append(item["text"])
            self._stats["speak"] += 1
        except asyncio.CancelledError:
            raise
        except Exception as e:
            import traceback as _tb
            console.error(
                f"主动发言消费出错（线程不退出）：{e}\n"
                f"{''.join(_tb.format_exception(type(e), e, e.__traceback__))}")
        finally:
            self._queue.task_done()


def _is_duplicate(self, text: str, kind: str) -> bool:
    """与最近会话 / 近期已播主动发言做相似度比对。"""
    threshold = max(0.0, min(1.0, float(self.cfg.AGENT_DUP_THRESHOLD or 0.85)))
    if threshold <= 0:
        return False
    recent: List[str] = []
    if self.mm is not None:
        for turn in self.mm.recent_turns[-self.cfg.AGENT_HISTORY_SNAPSHOT:]:
            content = (turn.get("content") or "").strip()
            if content:
                recent.append(content)
    if kind == "topic":
        recent.extend(self._recent_prompts)
    for recent_text in recent:
        if recent_text and SequenceMatcher(None, text, recent_text).ratio() >= threshold:
            return True
    return False


async def _speak_item(self, item: dict) -> None:
    """播报一条主动发言：输出锁互斥 + 拒收标记 + 播后状态回写。"""
    kind = item["kind"]
    text = item["text"]
    topic = item.get("topic")
    reason = "心里话" if kind == "emotional" else "话题"
    topic_tag = (f"｜灵感话题 {topic['category']}/{topic['id']}"
                 if topic is not None else "")
    console.dim(f"[主动] 自主开口（{reason}）...{topic_tag}"
                f"（{len(text)} 字）")

    self.speaking = True
    set_global_state(STATE_AGENT_THINKING)
    try:
        import traceback as _tb

        async def _store_memory() -> None:
            try:
                if kind == "emotional" and self.butler is not None:
                    await self.butler.submit_extract_and_store(
                        [{"role": "assistant", "content": text}],
                        self.mm.recent_turns if self.mm is not None else None,
                    )
                if self.mm is not None:
                    self.mm.add_turn(ROLE_AI_ALIAS, text,
                                     source=SOURCE_PROACTIVE)
            except Exception:
                pass

        if self.butler is not None or self.mm is not None:
            asyncio.create_task(_store_memory())

        console.dim(f"[主动] 等待输出锁…（当前锁状态："
                    f"locked={self._output_lock.locked()})")
        async with self._output_lock:
            self._speak_done.clear()
            set_output_owner("proactive")
            set_global_state(STATE_AI_SPEAKING)
            try:
                console.dim(f"[主动] 已拿到输出锁，开始播报")
                if self.tts is not None:
                    try:
                        self.tts.clear_interrupt()
                    except Exception:
                        pass
                await stream.speak_text(
                    text, self.tts, self.face, self.sub,
                    proactive=True,
                    profanity_filter=self.pf,
                    profanity_filter_rate=self.pf_rate,
                    emotion_actor=self.emotion_actor,
                )
                console.chat()
                console.dim(f"[主动] 播报完成")
            finally:
                set_output_owner(None)
                set_global_state(STATE_IDLE)
                self._speak_done.set()
    except Exception as e:
        console.error(
            f"主动发言失败：{e}\n"
            f"{''.join(_tb.format_exception(type(e), e, e.__traceback__))}")
    finally:
        self.speaking = False
        if topic is not None:
            _begin_topic(self, topic)
