"""全局输出互斥：三方说话者抢占 + 说话者身份标记。

用于实现两个需求：
1. 主动对话 / 弹幕回复 / 用户对话 三方互斥，同时间只有一方播报；
2. 当播报者是「主动对话」或「弹幕回复」时，输入监听层收到用户输入
   （键盘 / 语音识别）直接丢弃，不进入对话队列——保证这两类播报
   "不被任何输入打断 + 说话期间不接收信息"。
"""

import asyncio
from typing import Optional


# 全局输出互斥锁：三方抢占，谁拿到谁说话
_OUTPUT_LOCK = asyncio.Lock()

# 当前持有锁并正在播报的一方：
#   "user"      → 用户自己在说话（自己可以打断自己，不拒收输入）
#   "proactive" → 主动对话在播报（拒收任何输入）
#   "danmaku"   → 弹幕回复在播报（拒收任何输入）
#   None        → 当前无人说话
_OUTPUT_OWNER: Optional[str] = None


def get_output_lock() -> asyncio.Lock:
    return _OUTPUT_LOCK


def get_output_owner() -> Optional[str]:
    return _OUTPUT_OWNER


def set_output_owner(owner: Optional[str]) -> None:
    global _OUTPUT_OWNER
    _OUTPUT_OWNER = owner


def is_rejecting_input() -> bool:
    """当前是否正在主动说话 / 回复弹幕：是则拒收任何输入。"""
    return _OUTPUT_OWNER in ("proactive", "danmaku")
