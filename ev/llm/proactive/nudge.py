"""Nudge 契机引擎（Neuro-sama 风格，参考 新建文件夹/EV-Anthropomorphic 设计）。

灵感：Neuro-sama 看起来"随时说话"，实际是系统持续给 LLM 创造"说话契机"。
本模块在「事件到来 / 事件处理后」顺手检查 5 种契机，任一命中就推一个
NudgeEvent 给主动引擎 → 由主模型自主决定开口还是 [SILENT] 拒绝（不强制）。

与旧 heartbeat 模式的核心区别：
  - 旧：每 5~15s 醒一次就问一次 LLM「想不想说」→ token 烧在无意义的轮询上；
  - 新：只在契机命中时才问 LLM（long_silence 兜底保证冷场有救），
    平时心跳只做纯本地状态检查，零 LLM 调用。

设计原则（与参考实现一致）：
  - 0 个 sleep 循环：状态更新由 observe() 在事件线程内顺手做；
  - Nudge 是"建议"不是"命令"：LLM 可以拒绝，拒绝率被统计供自进化校准；
  - 防刷：全局冷却 + 同因重复抑制 + 60s 窗口上限。

5 种契机（按优先级）：
  1. state_change    状态切换（30s 内刚发生过）
  2. burst           弹幕爆发（窗口内 >= burst_threshold 条）
  3. many_unread     未读堆积（累积 >= many_unread_threshold 条未回应弹幕）
  4. silent_too_long AI 太久没说话（>= silent_too_long_sec）
  5. long_silence    直播间冷场（>= long_silence_sec，兜底）

事件类型（observe 接受）：
  - danmaku      观众弹幕到达（未读 +1、爆发窗口 +1、刷新活跃时间）
  - user_input   用户/键盘/语音发言（清未读、刷新活跃时间）
  - ai_spoke     AI 播报完成（刷新上次发言时间、清未读、新场景开始）
  - state_change 任意外部状态切换（如 engagement / 全局输出状态变化）
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Dict, List, Optional

from ev.utils import console


# ===== 契机类型 =====

class NudgeReason(str, Enum):
    LONG_SILENCE = "long_silence"        # 直播间冷场
    MANY_UNREAD = "many_unread"          # 未读弹幕堆积
    STATE_CHANGE = "state_change"       # 状态切换
    SILENT_TOO_LONG = "silent_too_long"  # AI 太久没说话
    BURST = "burst"                      # 弹幕爆发


# 触发时机敏感的契机：只有这两种允许携带「强制开口」兜底
# （PROACTIVE_FORCE_SPEAK=true 且静默超阈值时，防冷场）
_FORCIBLE_REASONS = frozenset({NudgeReason.LONG_SILENCE, NudgeReason.SILENT_TOO_LONG})


@dataclass
class NudgeEvent:
    """一次契机：reason + 上下文 + 给 LLM 看的自然语言提示。"""
    reason: NudgeReason
    ts: float = field(default_factory=time.time)
    context: dict = field(default_factory=dict)
    prompt_hint: str = ""

    def to_dict(self) -> dict:
        return {
            "reason": self.reason.value,
            "ts": self.ts,
            "context": self.context,
            "prompt_hint": self.prompt_hint,
        }


def _default_hints() -> Dict[NudgeReason, Callable[[dict], str]]:
    """契机 → prompt_hint 生成器（给 LLM 看的"人话"提示）。"""
    return {
        NudgeReason.LONG_SILENCE: lambda c:
            f"直播间已经安静 {c.get('silence_sec', 0):.0f} 秒了，"
            "你可以主动说点啥，或者继续潜水。",
        NudgeReason.MANY_UNREAD: lambda c:
            f"有 {c.get('unread', 0)} 条弹幕你还没回应，要不要看看？",
        NudgeReason.STATE_CHANGE: lambda c:
            f"氛围变化了：从 {c.get('from', '?')} 切到 {c.get('to', '?')}，"
            "你可以根据新氛围调整。",
        NudgeReason.SILENT_TOO_LONG: lambda c:
            f"你已经 {c.get('silent_sec', 0):.0f} 秒没说话了，"
            "可能该说点啥了（或者继续潜水）。",
        NudgeReason.BURST: lambda c:
            f"弹幕爆炸了，{c.get('window_sec', 0):.0f} 秒内 "
            f"{c.get('count', 0)} 条，想参与吗？",
    }


# ===== Nudge 引擎 =====

class NudgeEngine:
    """主动驱动引擎（完全事件驱动，0 定时器）。

    用法（E.V 接线后的实际路径）：
      - 弹幕到达 / 用户输入 / AI 播报完成 → engine.observe(type, payload)
        （observe 顺手做契机检查，命中则回调所有 listener）
      - 主循环心跳到点 → engine.check()（覆盖"没有任何事件"的纯冷场路径）
      - ProactiveEngine 收到契机 → 问 LLM → report_act / report_reject
    """

    def __init__(
        self,
        long_silence_sec: float = 30.0,
        silent_too_long_sec: float = 300.0,
        many_unread_threshold: int = 5,
        burst_threshold: int = 10,
        burst_window_sec: float = 30.0,
        nudge_cooldown_sec: float = 30.0,
        repeat_gap_sec: float = 60.0,
        state_change_window_sec: float = 30.0,
        window_cap: int = 3,
    ) -> None:
        # ---- 阈值配置 ----
        self.long_silence_sec = float(long_silence_sec)
        self.silent_too_long_sec = float(silent_too_long_sec)
        self.many_unread_threshold = int(many_unread_threshold)
        self.burst_threshold = int(burst_threshold)
        self.burst_window_sec = float(burst_window_sec)
        self.nudge_cooldown_sec = float(nudge_cooldown_sec)
        # 同因重复抑制：同一契机两次推送最小间隔（防 [SILENT] 后被反复打扰）
        self.repeat_gap_sec = float(repeat_gap_sec)
        # 状态切换发生后多久内算"新鲜契机"
        self.state_change_window_sec = float(state_change_window_sec)
        # 60s 滑动窗口内最多推送次数（防同分钟连环打扰）
        self.window_cap = int(window_cap)

        # ---- 事件状态（observe 更新）----
        self.last_activity_ts: float = time.time()  # 任何事件（弹幕/输入/AI 说话）
        self.last_ai_speak_ts: float = 0.0          # AI 上次播报完成时间
        self.unread_count: int = 0                  # 累积未回应弹幕
        self.burst_window: List[float] = []         # 弹幕到达时间（爆发检测）

        # ---- 状态切换记录 ----
        self.last_state_change_ts: float = 0.0
        self.last_state_change: tuple = (None, None)  # (from, to)

        # ---- 防刷状态 ----
        self.last_nudge_ts: float = 0.0
        self._last_fire_ts: Dict[NudgeReason, float] = {}
        self._window_start_ts: float = time.time()
        self._window_count: int = 0

        # ---- 监听器（同步回调：命中契机时推给主动引擎唤醒主循环）----
        self._listeners: List[Callable[[NudgeEvent], None]] = []

        # ---- 统计（自进化校准用）----
        self._stats: dict = {
            "nudge_total": 0,
            "nudge_by_reason": {r.value: 0 for r in NudgeReason},
            "nudge_acted": 0,      # LLM 接受契机（开口）
            "nudge_rejected": 0,   # LLM 拒绝契机（[SILENT]）
        }

    # ----- 监听器 -----

    def add_listener(self, cb: Callable[[NudgeEvent], None]) -> None:
        """注册契机监听器（主动引擎用它把契机转成一次心跳唤醒）。"""
        if cb not in self._listeners:
            self._listeners.append(cb)

    def clear_listeners(self) -> None:
        self._listeners.clear()

    # ----- 主入口：observe（事件到来时）-----

    def observe(self, event_type: str, payload: Optional[dict] = None) -> Optional[NudgeEvent]:
        """事件驱动入口：更新内部状态 + 顺手做契机检查。

        在弹幕/输入/AI 播报等事件现场调用（同事件循环线程，纯内存操作）。
        命中契机时回调 listener 并返回 NudgeEvent；未命中返回 None。
        """
        now = time.time()
        self._update_state(event_type, payload or {}, now)
        return self._check(now)

    # ----- 主入口：check（心跳到点 / 主动查询时）-----

    def check(self) -> Optional[NudgeEvent]:
        """无事件路径的契机检查（心跳超时唤醒 / nudge_check 查询）。

        与 observe 的区别：不更新事件状态，只做契机评估。
        """
        return self._check(time.time())

    # ----- 内部：状态更新 -----

    def _update_state(self, event_type: str, payload: dict, now: float) -> None:
        if event_type == "danmaku":
            self.last_activity_ts = now
            self.unread_count += 1
            self.burst_window.append(now)

        elif event_type == "user_input":
            self.last_activity_ts = now
            self.unread_count = 0  # 用户发言即回应，未读清零
            self.burst_window.clear()

        elif event_type == "ai_spoke":
            self.last_ai_speak_ts = now
            self.last_activity_ts = now
            self.unread_count = 0      # AI 回应了，未读清零
            self.burst_window.clear()

        elif event_type == "state_change":
            old = payload.get("from")
            new = payload.get("to")
            if old and new and old != new:
                self.last_state_change_ts = now
                self.last_state_change = (old, new)
                self.last_activity_ts = now

    # ----- 内部：契机评估 -----

    def _check(self, now: float) -> Optional[NudgeEvent]:
        # 1) 全局冷却（两次契机最小间隔）
        if now - self.last_nudge_ts < self.nudge_cooldown_sec:
            return None
        # 2) 60s 窗口上限（防同一分钟连环推送）
        if now - self._window_start_ts > 60:
            self._window_start_ts = now
            self._window_count = 0
        if self._window_count >= self.window_cap:
            return None
        # 3) 爆发窗口滑动清理
        cutoff = now - self.burst_window_sec
        self.burst_window = [t for t in self.burst_window if t > cutoff]

        # 4) 按优先级检查 5 种契机（同因 repeat_gap 内不重复推）
        nudge = None
        candidates: List[tuple] = []

        if (self.last_state_change_ts > 0
                and now - self.last_state_change_ts < self.state_change_window_sec):
            candidates.append((
                NudgeReason.STATE_CHANGE,
                {"from": self.last_state_change[0], "to": self.last_state_change[1]},
            ))

        if len(self.burst_window) >= self.burst_threshold:
            candidates.append((
                NudgeReason.BURST,
                {"count": len(self.burst_window), "window_sec": self.burst_window_sec},
            ))

        if self.unread_count >= self.many_unread_threshold:
            candidates.append((
                NudgeReason.MANY_UNREAD,
                {"unread": self.unread_count},
            ))

        if (self.last_ai_speak_ts > 0
                and (now - self.last_ai_speak_ts) > self.silent_too_long_sec):
            candidates.append((
                NudgeReason.SILENT_TOO_LONG,
                {"silent_sec": now - self.last_ai_speak_ts},
            ))

        # 冷场兜底：长时间无任何活动，且距 AI 上次发言有一定间隔
        quiet = now - self.last_activity_ts
        if quiet > self.long_silence_sec:
            ai_gap_ok = (self.last_ai_speak_ts == 0
                         or (now - self.last_ai_speak_ts) > 60)
            if ai_gap_ok:
                candidates.append((
                    NudgeReason.LONG_SILENCE,
                    {"silence_sec": quiet},
                ))

        for reason, context in candidates:
            last_fire = self._last_fire_ts.get(reason, 0.0)
            if now - last_fire < self.repeat_gap_sec:
                continue
            nudge = self._make_nudge(reason, context)
            break

        if nudge is None:
            return None

        # 5) 记录推送 + 通知监听器
        self.last_nudge_ts = now
        self._last_fire_ts[nudge.reason] = now
        self._window_count += 1
        self._stats["nudge_total"] += 1
        self._stats["nudge_by_reason"][nudge.reason.value] += 1
        console.dim(f"[契机] {nudge.reason.value}：{nudge.prompt_hint}")
        for cb in list(self._listeners):
            try:
                cb(nudge)
            except Exception as e:
                console.dim(f"[契机] 监听器出错（忽略）：{e}")
        return nudge

    def _make_nudge(self, reason: NudgeReason, context: dict) -> NudgeEvent:
        hint = _default_hints().get(reason, lambda c: "该说点啥了？")(context)
        return NudgeEvent(reason=reason, context=context, prompt_hint=hint)

    # ----- 反馈（LLM 决定后由主动引擎回调）-----

    def report_act(self) -> None:
        """LLM 接受契机（申请开口并成功入队）。"""
        self._stats["nudge_acted"] += 1

    def report_reject(self) -> None:
        """LLM 拒绝契机（[SILENT] / 生成失败）。"""
        self._stats["nudge_rejected"] += 1

    def is_forcible(self, reason: NudgeReason) -> bool:
        """该契机是否允许携带强制开口兜底（仅冷场/太久没说）。"""
        return reason in _FORCIBLE_REASONS

    # ----- 状态/统计查询 -----

    def get_stats(self) -> dict:
        return {
            "nudge_total": self._stats["nudge_total"],
            "nudge_by_reason": dict(self._stats["nudge_by_reason"]),
            "nudge_acted": self._stats["nudge_acted"],
            "nudge_rejected": self._stats["nudge_rejected"],
        }

    def get_state(self) -> dict:
        return {
            "quiet_sec": max(0.0, time.time() - self.last_activity_ts),
            "since_ai_spoke_sec": (
                max(0.0, time.time() - self.last_ai_speak_ts)
                if self.last_ai_speak_ts > 0 else None),
            "unread_count": self.unread_count,
            "burst_count": len(self.burst_window),
            "last_nudge_ts": self.last_nudge_ts,
            "last_state_change": self.last_state_change,
        }

    def should_speak_now(self) -> tuple:
        """综合判断当前是否该给开口机会：(是否该说, 契机事件或 None)。"""
        nudge = self.check()
        return (nudge is not None, nudge)


# ===== 全局单例（弹幕线程 / 主循环 / 主动引擎共享）=====

_engine: Optional[NudgeEngine] = None


def get_engine() -> NudgeEngine:
    """获取全局契机引擎（未初始化时以默认参数懒创建）。"""
    global _engine
    if _engine is None:
        _engine = NudgeEngine()
    return _engine


def ensure_engine(**kwargs) -> NudgeEngine:
    """确保引擎存在并按配置校准阈值；已存在时仅更新阈值（支持热更）。"""
    global _engine
    if _engine is None:
        _engine = NudgeEngine(**kwargs)
    else:
        for key, val in kwargs.items():
            if hasattr(_engine, key) and val is not None:
                setattr(_engine, key, val)
    return _engine


def reset_engine() -> None:
    """重置全局实例（供测试）。"""
    global _engine
    _engine = None


def observe(event_type: str, payload: Optional[dict] = None) -> Optional[NudgeEvent]:
    """模块级便捷入口：外部事件源（弹幕 client 等）直接调用。

    引擎未初始化时懒创建（默认参数），保证埋点零配置可用。
    """
    return get_engine().observe(event_type, payload)
