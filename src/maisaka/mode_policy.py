"""Maisaka 模式与平台策略。"""

from typing import Optional

# 直播平台名，与 world-bilibili 插件注入弹幕时使用的 platform 保持一致
LIVE_PLATFORM = "bilibili_live"

IDLE_CYCLE_REASONS = {
    "planner_no_tool_end",
    "planner_wait_rest",
    "tool_pause:wait",
    "tool_stop_after_execution",
}


def is_idle_cycle_reason(cycle_end_reason: str) -> bool:
    """判断整轮结束原因是否属于空闲退避。"""

    return str(cycle_end_reason).strip() in IDLE_CYCLE_REASONS


def is_live_platform(platform: Optional[str]) -> bool:
    """判断聊天流平台是否属于直播平台。"""

    return str(platform or "").startswith(LIVE_PLATFORM)