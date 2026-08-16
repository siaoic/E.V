"""全局输出互斥：三方说话者抢占 + 说话者身份标记。

用于实现两个需求：
1. 主动对话 / 弹幕回复 / 用户对话 / 任务 Agent 四方互斥，同时间只有一方
   说话（Agent 任务执行同样占用锁，期间不开口说话但禁止被输入打断）；
2. 当播报者是「主动对话」或「弹幕回复」、或任务 Agent 执行中，输入监听
   层收到用户输入（键盘 / 语音识别）直接丢弃，不进入对话队列——保证
   "不被任何输入打断 + 说话期间不接收信息"。
"""

import asyncio
import threading
from typing import Optional


# 全局输出互斥锁：多方抢占，谁拿到谁说话
_OUTPUT_LOCK = asyncio.Lock()

# 当前持有锁并正在播报的一方：
#   "user"      → 用户自己在说话（自己可以打断自己，不拒收输入）
#   "proactive" → 主动对话在播报（拒收任何输入）
#   "danmaku"   → 弹幕回复在播报（拒收任何输入）
#   "agent"     → 任务 Agent 执行中（拒收任何输入）
#   None        → 当前无人说话
_OUTPUT_OWNER: Optional[str] = None

# ===== 全局状态机（忙碌抑制 agent 触发 + LLM 智能避让） =====
# 取值：
#   STATE_IDLE            → 空闲：唯一允许 agent 触发 LLM 的状态
#   STATE_USER_TALKING    → 用户输入已到达（正在处理，AI 尚未开始回复）
#   STATE_AI_SPEAKING     → AI 正在播报（用户对话 / 弹幕回复 / 主动发言）
#   STATE_AGENT_THINKING  → 主动对话 agent 正在生成发言（LLM 推理中，尚未开口）
#   STATE_AGENT_RUNNING   → 任务 Agent 正在执行（!agent 任务，与前者区分）
# 状态只由单线程（主事件循环）读写，加锁仅为防御性（跨线程读取）。
STATE_IDLE = "idle"
STATE_USER_TALKING = "user_talking"
STATE_AI_SPEAKING = "ai_speaking"
STATE_AGENT_THINKING = "agent_thinking"
STATE_AGENT_RUNNING = "agent_running"
_VALID_STATES = (STATE_IDLE, STATE_USER_TALKING, STATE_AI_SPEAKING,
                 STATE_AGENT_THINKING, STATE_AGENT_RUNNING)
_global_state = STATE_IDLE
_state_lock = threading.Lock()


def get_output_lock() -> asyncio.Lock:
    return _OUTPUT_LOCK


def get_output_owner() -> Optional[str]:
    return _OUTPUT_OWNER


def set_output_owner(owner: Optional[str]) -> None:
    global _OUTPUT_OWNER
    _OUTPUT_OWNER = owner


def is_rejecting_input() -> bool:
    """当前是否正在主动说话 / 回复弹幕 / 执行 Agent 任务：是则拒收任何输入。"""
    return _OUTPUT_OWNER in ("proactive", "danmaku", "agent")


def set_agent_owner() -> None:
    """任务 Agent 占用输出锁：标记 owner + 全局状态为 AGENT_RUNNING。

    配合 Agent 主循环的 async with get_output_lock() 使用，期间用户输入
    被 is_rejecting_input 丢弃；任务结束由调用方 set_output_owner(None)
    + set_global_state(STATE_IDLE) 释放。
    """
    set_output_owner("agent")
    set_global_state(STATE_AGENT_RUNNING)


def set_global_state(state: str) -> None:
    """设置全局状态（仅接受合法状态值）；实际变化时广播 EV_STATE_CHANGE。"""
    global _global_state
    if state not in _VALID_STATES:
        return
    with _state_lock:
        old = _global_state
        if old == state:
            return
        _global_state = state
    _emit_state_change(old, state)


def _emit_state_change(old: str, new: str) -> None:
    """向事件总线广播状态变化（fire-and-forget；非事件循环环境静默跳过）。

    延迟 import 避免 output_lock ↔ bus 循环依赖。
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return  # 启动阶段等非事件循环环境，跳过广播
    from src.core.bus import EV_STATE_CHANGE, bus
    from src.core.events.models import StateChangeEvent
    loop.create_task(bus.emit(
        EV_STATE_CHANGE, StateChangeEvent(state=new, previous=old)))


def get_global_state() -> str:
    """读取当前全局状态。"""
    with _state_lock:
        return _global_state


def is_idle() -> bool:
    """是否空闲（空闲状态才允许 agent 触发 LLM）。"""
    with _state_lock:
        return _global_state == STATE_IDLE


def is_busy() -> bool:
    """是否忙碌（非空闲即忙碌：用户/AI 正在处理，抑制 agent 触发）。"""
    with _state_lock:
        return _global_state != STATE_IDLE


# 弹幕回复「已敲定、待播报」标记：picker 选出到播报完成期间置位。
# 主动对话据此避让——回复弹幕优先于主动搭话，避免话痨抢话/吞弹幕。
_danmaku_pending = False


def set_danmaku_pending(pending: bool) -> None:
    """置位/清除「弹幕回复待播报」标记（弹幕精选回调置位，回复播报完成清除）。"""
    global _danmaku_pending
    with _state_lock:
        _danmaku_pending = pending


def is_danmaku_pending() -> bool:
    """是否有弹幕回复已敲定但尚未播报完成。"""
    with _state_lock:
        return _danmaku_pending
