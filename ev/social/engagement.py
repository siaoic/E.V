"""ev.social.engagement — 完全事件驱动的参与度状态机(无 tick_loop)

灵感:qq-bridge 完全不用 setInterval,只在事件触发时评估。
E.V 改造:
  - 不再有 8s tick_loop
  - 状态在「事件到来」时被评估/更新
  - 主动说话由 AI 调 request_speak 工具触发(见 wake.py)
  - 潜水由 AI 调 set_wake_config 触发(见 wake.py)

5 个状态保持兼容:
  - observe: 每条事件都参与(默认)
  - active:  热闹时(事件密集)
  - probe:   试探(偶尔主动)
  - exit:    退场(几乎不主动)
  - sleep:   沉睡(由 setWakeConfig 实现,不是状态机本身)

事件类型(触发评估):
  - EV_DANMAKU_RECV: 弹幕
  - EV_INPUT_RECV: 用户输入
  - EV_USER_INTERACTION: 用户互动(给礼物、SC)
  - EV_AGENT_REQUEST: AI 自己调工具触发的"我想说话了"
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, asdict, field
from enum import Enum
from pathlib import Path
from typing import Optional

logger = logging.getLogger("ev.social.engagement")


# ===== 5 个状态(兼容老版本,但 sleep 由 wake.py 接管) =====
STATE_OBSERVE = "observe"
STATE_ACTIVE = "active"
STATE_PROBE = "probe"
STATE_EXIT = "exit"
STATE_SLEEP = "sleep"  # 保留兼容,但实际逻辑在 wake.py

_VALID_STATES = {STATE_OBSERVE, STATE_ACTIVE, STATE_PROBE, STATE_EXIT, STATE_SLEEP}


@dataclass
class EngagementState:
    current: str = STATE_OBSERVE
    since_ts: float = field(default_factory=time.time)
    last_transition_reason: str = "init"
    history: list = field(default_factory=list)
    
    # 事件计数(由 on_event 更新)
    events_last_min: list = field(default_factory=list)  # [(ts, type), ...]
    silence_count_recent: int = 0
    danmaku_count_last_min: int = 0
    
    # 阈值
    active_density: int = 5
    exit_after_sec: int = 180
    sleep_hour: int = 24
    
    def to_dict(self) -> dict:
        return asdict(self)
    
    @classmethod
    def from_dict(cls, d: dict) -> "EngagementState":
        return cls(**{k: d[k] for k in d if k in cls.__dataclass_fields__})


# ===== 全局单例 =====
_state: EngagementState = EngagementState()
_state_lock = None  # lazy init


def get_state() -> str:
    return _state.current


async def transition_to(new_state: str, reason: str = "") -> bool:
    """状态迁移。"""
    global _state
    if new_state not in _VALID_STATES:
        logger.warning(f"[engagement] invalid state: {new_state}")
        return False
    if new_state == _state.current:
        return False
    
    old = _state.current
    _state.current = new_state
    _state.since_ts = time.time()
    _state.last_transition_reason = reason
    _state.history.append({
        "from": old, "to": new_state, "ts": _state.since_ts, "reason": reason,
    })
    if len(_state.history) > 100:
        _state.history = _state.history[-100:]
    
    logger.info(f"[engagement] {old} → {new_state} ({reason})")
    
    # ⭐ 通知 nudge:状态切换是个"契机"（observe(event_type, payload)，
    #    与 ev.llm.proactive.nudge 的公开埋点入口一致）
    try:
        from .nudge import get_engine
        get_engine().observe("state_change", {
            "from": old, "to": new_state, "reason": reason})
    except Exception:
        pass
    
    await _persist()
    return True


def is_proactive_allowed() -> bool:
    """是否允许主动说话(由状态机判定)。
    
    注意:即使这里返回 True,实际是否说话还是由 AI 调 request_speak 决定。
    这里只是"环境是否允许"。
    """
    # 如果 wake.py 在潜水,主动说话不允许
    try:
        from .wake import is_sleeping
        if is_sleeping():
            return False
    except Exception:
        pass
    
    return _state.current in (STATE_OBSERVE, STATE_ACTIVE, STATE_PROBE)


def is_engagement_silent() -> bool:
    return _state.current in (STATE_SLEEP, STATE_EXIT)


def get_dialogue_openness() -> float:
    """综合态 + 情绪,得到"现在愿意说话的程度" 0~1。"""
    base = {
        STATE_OBSERVE: 0.5,
        STATE_ACTIVE: 0.9,
        STATE_PROBE: 0.3,
        STATE_EXIT: 0.1,
        STATE_SLEEP: 0.0,
    }.get(_state.current, 0.5)
    
    try:
        from ev.emotion.state import VADState
        emotion = VADState().dominant_emotion() or "中性"
    except Exception:
        emotion = "中性"
    
    mood_modifier = {
        "开心": 0.2, "happy": 0.2, "excited": 0.3, "中性": 0.0, "neutral": 0.0,
        "悲伤": -0.3, "tired": -0.2, "sad": -0.3,
        "生气": -0.1, "angry": -0.1,
    }.get(emotion, 0.0)
    
    return max(0.0, min(1.0, base + mood_modifier))


# ===== 核心:事件驱动的状态评估 =====

async def on_event(event_type: str, payload: Optional[dict] = None) -> None:
    """任何事件到来时调用,根据事件类型 + 当前状态评估是否需要迁移。
    
    这是「完全事件驱动」的核心 —— 没有 tick_loop,只在事件触发时评估。
    
    Args:
        event_type: "danmaku" / "user_input" / "user_interaction" / "agent_request" / ...
        payload: 事件数据(可选)
    """
    now = time.time()
    
    # 1) 记录事件
    _state.events_last_min.append((now, event_type))
    # 滚动清理
    _state.events_last_min = [
        (ts, t) for ts, t in _state.events_last_min if now - ts < 60
    ]
    _state.danmaku_count_last_min = sum(
        1 for ts, t in _state.events_last_min if t == "danmaku" and now - ts < 60
    )
    
    # 2) 时间判定(深夜 → sleep)
    if 0 < _state.sleep_hour <= 23:
        hour = time.localtime(now).tm_hour
        if hour >= _state.sleep_hour and _state.current != STATE_SLEEP:
            await transition_to(STATE_SLEEP, reason=f"sleep_hour={_state.sleep_hour}")
            return
    
    # 3) 状态迁移
    density = _state.danmaku_count_last_min
    
    if _state.current == STATE_OBSERVE:
        if density > _state.active_density:
            await transition_to(STATE_ACTIVE, reason=f"density>{_state.active_density}")
        elif event_type == "agent_request" and (now - _state.since_ts) > 60:
            # AI 在 observe 态主动请求说话 → 切到 active
            await transition_to(STATE_ACTIVE, reason="agent_self_request")
    
    elif _state.current == STATE_ACTIVE:
        if density == 0 and (now - _state.since_ts) > _state.exit_after_sec:
            await transition_to(STATE_EXIT, reason=f"density=0_for_{_state.exit_after_sec}s")
    
    elif _state.current == STATE_PROBE:
        if density > 2:
            await transition_to(STATE_OBSERVE, reason="density_back>2")
    
    elif _state.current == STATE_EXIT:
        if density > 3:
            await transition_to(STATE_ACTIVE, reason="called_back_density>3")
        elif density < 0.5 and (now - _state.since_ts) > 300:
            await transition_to(STATE_PROBE, reason="cooling_to_probe")
    
    # 4) 通知 wake(让 wait_for_window 立即返回)
    try:
        from .wake import poke
        poke()
    except Exception:
        pass
    
    # 5) 通知 nudge:状态切换是个"契机"
    try:
        from .nudge import get_engine
        # 状态切换事件由 nudge 自己从 history 检测
        # 这里只是确保 nudge 知道状态变了
    except Exception:
        pass


# ===== 兼容老的主动心跳 API(留给主循环显式调用) =====
async def on_tick() -> None:
    """兼容老的 tick 入口,但内部不做事(完全事件驱动)。
    
    主循环如果还在显式调 engagement.on_tick(),会走这个空实现。
    实际状态迁移由 on_event 触发。
    """
    # 没有任何 sleep / loop
    # 只在事件触发时才评估
    pass


# ===== 持久化 =====
_ENGAGEMENT_PATH: Optional[Path] = None


def _resolve_path() -> Path:
    global _ENGAGEMENT_PATH
    if _ENGAGEMENT_PATH:
        return _ENGAGEMENT_PATH
    try:
        from ev.utils import config as _cfg
        p = Path(_cfg.cfg.DATA_ROOT) / "social" / "engagement_state.json"
    except Exception:
        p = Path("data/social/engagement_state.json")
    p.parent.mkdir(parents=True, exist_ok=True)
    _ENGAGEMENT_PATH = p
    return p


async def _persist() -> None:
    try:
        p = _resolve_path()
        p.write_text(json.dumps(_state.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        logger.warning(f"[engagement] persist failed: {e}")


async def load_persisted() -> None:
    global _state
    p = _resolve_path()
    if not p.exists():
        return
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        _state = EngagementState.from_dict(d)
        logger.info(f"[engagement] loaded state={_state.current}")
    except Exception as e:
        logger.warning(f"[engagement] load failed: {e}")


# ===== 配置 =====
def apply_config(cfg: dict) -> None:
    _state.active_density = int(cfg.get("SOCIAL_ENGAGEMENT_ACTIVE_DENSITY", 5))
    _state.exit_after_sec = int(cfg.get("SOCIAL_ENGAGEMENT_EXIT_AFTER", 180))
    _state.sleep_hour = int(cfg.get("SOCIAL_ENGAGEMENT_SLEEP_HOUR", 24))
    default = cfg.get("SOCIAL_ENGAGEMENT_DEFAULT_STATE", STATE_OBSERVE)
    if default in _VALID_STATES:
        _state.current = default


# ===== 旧 API 兼容(给主循环 / proactive 用) =====
def note_passed() -> None:
    """deliberation 跳过一条弹幕(计数)。事件驱动的副作用统计。"""
    pass  # 由 on_event 自动统计


def note_silence() -> None:
    """[SILENT] 触发了(计数)。"""
    _state.silence_count_recent += 1
