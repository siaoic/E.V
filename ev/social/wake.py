"""ev.social.wake — 完全事件驱动的"潜水 / 唤醒"机制

灵感:qq-bridge 的 setWakeConfig 工具 + wait_for_messages。
AI 自主决定"什么时候睡、睡多久、什么条件叫醒",程序只是个执行器。

核心设计:
  - 没有 asyncio.sleep 循环
  - 用「单次 wait_for + Event」实现"等到 X 时间或被事件打断"
  - 主循环的"心跳"是事件本身,不是定时器

唤醒模型:
  1. AI 调 set_wake_config 工具:
       - "我要潜水到 ts=T"
       - "无限期潜水(等 @ 我时叫醒)"
       - "现在立刻醒(取消潜水)"
  2. 写入 data/social/wake_config.json
  3. 主循环进入 wait_for_window(),内部 asyncio.wait_for(event, timeout=剩余秒数)
  4. 任一事件触发(弹幕/输入)→ event.set() → 立即返回
  5. timeout 到期 → 推 EV_WAKE_DEADLINE 事件,AI 自己决定说啥
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, asdict, field
from enum import Enum
from pathlib import Path
from typing import Optional, Callable, Awaitable

logger = logging.getLogger("ev.social.wake")


# ===== 唤醒模式 =====
class WakeMode(str, Enum):
    ACTIVE = "active"         # 不潜水,每个事件都考虑是否要说话
    DIVING = "diving"         # 潜水,只在被唤醒条件触发时考虑说话
    INFINITE = "infinite"     # 无限期潜水,只有明确条件才醒


@dataclass
class WakeConfig:
    mode: str = WakeMode.ACTIVE.value
    wake_at: float = 0.0        # unix ts,0=立即可醒
    sleep_seconds: int = 0      # 0=无限制
    triggers: dict = field(default_factory=dict)
    # triggers 字段:
    #   at_mention: bool      @ 我时醒
    #   name_mention: bool    叫我名字时醒
    #   keywords: list[str]   命中关键词时醒
    #   question: bool        提问时醒
    #   poke: bool            戳一顿时醒
    #   probability: float    普通消息随机唤醒概率(默认 0.05)
    
    def to_dict(self) -> dict:
        return asdict(self)
    
    @classmethod
    def from_dict(cls, d: dict) -> "WakeConfig":
        return cls(**{k: d[k] for k in d if k in cls.__dataclass_fields__})


# ===== 全局状态 =====
_wake: WakeConfig = WakeConfig()
_wake_lock = asyncio.Lock()
_event: asyncio.Event = asyncio.Event()  # 用于"立即打断 wait_for"
_listeners: list = []  # 监听 wake 状态变化的回调
# 主循环句柄（bootstrap 时注册）：poke 可能从弹幕线程调用，
# asyncio.Event.set() 非线程安全——跨线程时必须经 call_soon_threadsafe
_main_loop: Optional[object] = None


def set_main_loop(loop) -> None:
    """注册主事件循环（bootstrap 在主循环内调用一次）。"""
    global _main_loop
    _main_loop = loop


def get_wake_config() -> WakeConfig:
    return _wake


def is_diving() -> bool:
    """当前是否处于潜水状态。"""
    return _wake.mode != WakeMode.ACTIVE.value


def is_sleeping() -> bool:
    """当前是否在 sleep 区间内(还没到 wake_at)。"""
    if _wake.mode == WakeMode.ACTIVE:
        return False
    if _wake.wake_at == 0:
        return False
    return time.time() < _wake.wake_at


# ===== 核心 API =====

async def set_wake_config(
    mode: str = "diving",
    sleep_seconds: int = 0,
    wake_at: float = 0.0,
    triggers: Optional[dict] = None,
    reason: str = "",
) -> WakeConfig:
    """设置唤醒配置(AI 主动调用)。
    
    Args:
        mode: "active" / "diving" / "infinite"
        sleep_seconds: 睡多少秒(0=无限或由 wake_at 决定)
        wake_at: 绝对唤醒时间(unix ts,0=不设)
        triggers: 唤醒条件字典
        reason: 为什么这么设(供日志)
    
    Returns:
        设置后的 WakeConfig
    
    用例:
        await set_wake_config("diving", sleep_seconds=300, 
                              triggers={"at_mention": True, "name_mention": True},
                              reason="直播间安静了,潜水 5 分钟")
    """
    global _wake
    
    if triggers is None:
        triggers = {
            "at_mention": True,
            "name_mention": True,
            "question": True,
            "poke": True,
            "probability": 0.05,
        }
    
    if wake_at == 0 and sleep_seconds > 0:
        wake_at = time.time() + sleep_seconds
    
    async with _wake_lock:
        old = _wake
        _wake = WakeConfig(
            mode=mode,
            wake_at=wake_at,
            sleep_seconds=sleep_seconds,
            triggers=triggers,
        )
        await _persist()
    
    logger.info(
        f"[wake] config changed: {old.mode} → {mode}, "
        f"sleep={sleep_seconds}s, reason='{reason}'"
    )
    
    # 通知所有监听者
    for cb in list(_listeners):
        try:
            await cb(_wake)
        except Exception as e:
            logger.debug(f"[wake] listener error: {e}")
    
    # 立即打断 wait_for(让主循环重新评估)
    _event.set()
    
    return _wake


async def cancel_wake() -> None:
    """立即取消潜水,回到 active 模式。"""
    await set_wake_config(mode="active", reason="manual_wakeup")


def poke() -> None:
    """外部事件"戳一下",让 wait_for 立即返回(不持久化配置)。

    可能从任意线程调用（弹幕线程/主循环）：已注册主循环且当前不在
    主循环线程时，经 call_soon_threadsafe 投递（Event.set 非线程安全）。
    """
    if _main_loop is not None:
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is not _main_loop:
            try:
                _main_loop.call_soon_threadsafe(_event.set)
                return
            except RuntimeError:
                pass  # 主循环已关闭：就地 set 兜底
    _event.set()


async def check_should_wake(triggers: dict) -> bool:
    """检查当前事件是否应该唤醒 AI(用于"被叫到"的判定)。
    
    Args:
        triggers: 事件携带的触发信号,如
            {"is_at_me": True, "mentions_name": False, "is_question": True}
    
    Returns:
        True 表示应该唤醒
    """
    if _wake.mode == WakeMode.ACTIVE:
        return True
    
    t = _wake.triggers
    
    # 命中"硬触发"条件 → 必醒
    if triggers.get("is_at_me") and t.get("at_mention"):
        return True
    if triggers.get("mentions_name") and t.get("name_mention"):
        return True
    if triggers.get("is_question") and t.get("question"):
        return True
    if triggers.get("is_poke") and t.get("poke"):
        return True
    if triggers.get("matched_keyword") and t.get("keywords"):
        if triggers["matched_keyword"] in t["keywords"]:
            return True
    
    # 概率性唤醒
    import random
    prob = t.get("probability", 0.05)
    if prob > 0 and random.random() < prob:
        return True
    
    return False


# ===== 主循环使用的"等待窗口" =====

async def wait_for_window(timeout_sec: Optional[float] = None) -> str:
    """等待到下一个唤醒时刻 / 被打断。
    
    主循环在处理完一个事件后调用,有两种返回:
      - "woken": 被事件打断(有新消息)
      - "deadline": 时间到了(sleep 结束)
    
    Args:
        timeout_sec: 等待秒数(None=自动算剩余 sleep 时间)
    
    Returns:
        "woken" / "deadline"
    
    实现:
      单次 asyncio.wait_for(event.wait(), timeout=...) 
      → 没有 setInterval 循环
    """
    global _event
    _event.clear()
    
    if _wake.mode == WakeMode.ACTIVE:
        return "woken"  # active 模式不等,立即返回
    
    if _wake.wake_at > 0:
        remaining = _wake.wake_at - time.time()
        if remaining <= 0:
            return "deadline"  # 已经过期,无需等
        timeout_sec = min(remaining, timeout_sec) if timeout_sec else remaining
    
    if timeout_sec is None or timeout_sec <= 0:
        return "deadline"
    
    try:
        await asyncio.wait_for(_event.wait(), timeout=timeout_sec)
        return "woken"
    except asyncio.TimeoutError:
        return "deadline"


# ===== 持久化 =====
_WAKE_PATH: Optional[Path] = None


def _resolve_path() -> Path:
    global _WAKE_PATH
    if _WAKE_PATH:
        return _WAKE_PATH
    try:
        from ev.utils import config as _cfg
        p = Path(_cfg.cfg.DATA_ROOT) / "social" / "wake_config.json"
    except Exception:
        p = Path("data/social/wake_config.json")
    p.parent.mkdir(parents=True, exist_ok=True)
    _WAKE_PATH = p
    return p


async def _persist() -> None:
    try:
        p = _resolve_path()
        p.write_text(json.dumps(_wake.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        logger.warning(f"[wake] persist failed: {e}")


async def load_persisted() -> None:
    """从磁盘恢复(bootstrap 时调用)。"""
    global _wake
    p = _resolve_path()
    if not p.exists():
        return
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        _wake = WakeConfig.from_dict(d)
        logger.info(f"[wake] loaded: mode={_wake.mode}, wake_at={_wake.wake_at}")
    except Exception as e:
        logger.warning(f"[wake] load failed: {e}")


# ===== 监听器 =====
def add_listener(cb) -> None:
    """注册 wake 状态变化的监听器。
    
    cb 是 async callable,签名:async def cb(wake_config: WakeConfig) -> None
    """
    _listeners.append(cb)


def clear_listeners() -> None:
    _listeners.clear()


# ===== 工具函数(给 MCP 暴露) =====
def get_tool_definitions() -> list:
    """返回可暴露给 LLM 的工具定义(给 MCP server 用)。"""
    return [
        {
            "name": "set_wake_config",
            "description": (
                "设置你自己的唤醒/潜水策略。"
                "当你不想被打扰时,设 diving 模式并指定 sleep_seconds;"
                "triggers 决定什么事件会叫醒你(被 @、叫名字、提问、戳一下、命中关键词,以及普通消息的随机唤醒概率)。"
                "想回到 active 模式时,调 cancel_wake。"
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "mode": {
                        "type": "string",
                        "enum": ["active", "diving", "infinite"],
                        "description": "active=正常参与;diving=潜水但能被触发条件叫醒;infinite=无限期潜水",
                    },
                    "sleep_seconds": {
                        "type": "integer",
                        "description": "潜水秒数,0=不设时间限制(由 triggers 决定何时醒)",
                    },
                    "triggers": {
                        "type": "object",
                        "description": "触发条件字典",
                        "properties": {
                            "at_mention": {"type": "boolean", "description": "@ 我时醒"},
                            "name_mention": {"type": "boolean", "description": "叫我名字时醒"},
                            "question": {"type": "boolean", "description": "提问时醒"},
                            "poke": {"type": "boolean", "description": "戳一顿时醒"},
                            "probability": {"type": "number", "description": "普通消息随机唤醒概率,0~1"},
                            "keywords": {"type": "array", "items": {"type": "string"}, "description": "命中关键词时醒"},
                        },
                    },
                    "reason": {
                        "type": "string",
                        "description": "为什么这么设(供日志/自进化分析)",
                    },
                },
                "required": ["mode"],
            },
        },
        {
            "name": "cancel_wake",
            "description": "立刻取消潜水,回到 active 模式(每个事件都会考虑要不要说话)。",
            "input_schema": {
                "type": "object",
                "properties": {},
            },
        },
        {
            "name": "request_speak",
            "description": (
                "主动申请一次发言机会。"
                "当你潜水了一会儿,觉得该说点什么了,调这个工具;"
                "系统会给你一次 '自由发挥' 的机会(生成一段主动发言),用完即止。"
                "如果你是因为看到 nudge(直播间状态变化)才想说话,可以传 nudge_reason 让系统记录。"
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "topic_hint": {
                        "type": "string",
                        "description": "想聊什么话题(可选,不填就让系统挑)",
                    },
                    "reason": {
                        "type": "string",
                        "description": "为什么想说话(供日志)",
                    },
                    "nudge_reason": {
                        "type": "string",
                        "description": "如果是系统 nudge 触发的,填 nudge 原因(long_silence/many_unread/state_change/silent_too_long/burst 等)",
                    },
                },
            },
        },
        {
            "name": "nudge_check",
            "description": (
                "主动检查现在是不是该说话的时机。"
                "返回 should_speak=True 时,系统认为有契机(冷场/累积/状态变化/沉默太久/弹幕爆炸);"
                "你可以接着调 request_speak,或者用 [SILENT] 拒绝。"
                "LLM 也可以在每次想说话前调这个,作为'想自己评估时机'的工具。"
            ),
            "input_schema": {
                "type": "object",
                "properties": {},
            },
        },
    ]


async def handle_tool_call(name: str, args: dict) -> dict:
    """处理 LLM 调用的工具(MCP server 调用这个函数)。"""
    if name == "set_wake_config":
        cfg = await set_wake_config(
            mode=args.get("mode", "diving"),
            sleep_seconds=int(args.get("sleep_seconds", 0)),
            triggers=args.get("triggers"),
            reason=args.get("reason", ""),
        )
        return {"ok": True, "config": cfg.to_dict()}
    
    elif name == "cancel_wake":
        await cancel_wake()
        return {"ok": True, "mode": "active"}
    
    elif name == "request_speak":
        # 委托给 proactive 模块
        try:
            from .proactive import request_speak as _request_speak
            result = await _request_speak(
                topic_hint=args.get("topic_hint", ""),
                reason=args.get("reason", ""),
                nudge_reason=args.get("nudge_reason", ""),
            )
            return result
        except Exception as e:
            logger.exception(f"[wake] request_speak failed: {e}")
            return {"ok": False, "error": str(e)}
    
    elif name == "nudge_check":
        # ⭐ 手动触发 nudge check(供 LLM "想自己检查契机"时调)
        try:
            from .proactive import get_proactive_engine
            engine = get_proactive_engine()
            if engine is not None and hasattr(engine, "nudge_check"):
                return engine.nudge_check()
            # 无引擎时的兜底：直接查全局契机引擎（check 不吃参数）
            from .nudge import get_engine
            nudge = get_engine().check()
            if nudge:
                return {
                    "ok": True,
                    "should_speak": True,
                    "reason": nudge.reason.value,
                    "hint": nudge.prompt_hint,
                    "context": nudge.context,
                }
            return {"ok": True, "should_speak": False, "hint": "没有契机,继续潜水/正常处理"}
        except Exception as e:
            return {"ok": False, "error": str(e)}
    
    return {"ok": False, "error": f"unknown tool: {name}"}
