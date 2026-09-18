"""B 站直播间世界的内部实现包。

本包是「B 站直播间世界」插件（``maibot-live.world-bilibili``）的内部实现，
分三层：

- :mod:`~.client`：blivedm 连接层，只负责「连上去、把消息规范化后投进队列」；
- :mod:`~.render`：结构化的中文渲染层，blivedm 消息 → :class:`LiveEvent`；
- :mod:`~.stats`：状态统计与快照渲染。

对框架的公开契约通过插件组件（API）暴露，其他插件不应直接 import 本包。
"""

from __future__ import annotations

from .client import BiliDanmakuClient
from .render import (
    LiveChatMessage,
    LiveEvent,
    LiveEventKind,
    coin_to_yuan,
    describe_connection,
    guard_name,
    render_danmaku,
    render_gift,
    render_guard,
    render_interact,
    render_super_chat,
    render_user_toast,
)
from .stats import LiveRoomStats

__all__ = [
    "BiliDanmakuClient",
    "LiveChatMessage",
    "LiveEvent",
    "LiveEventKind",
    "LiveRoomStats",
    "coin_to_yuan",
    "describe_connection",
    "guard_name",
    "render_danmaku",
    "render_gift",
    "render_guard",
    "render_interact",
    "render_super_chat",
    "render_user_toast",
]
