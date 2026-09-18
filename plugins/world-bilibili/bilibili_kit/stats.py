"""直播间状态统计与快照渲染。

统计口径只记「互动」：礼物 / 上舰 / 进场关注。
弹幕与醒目留言不进快照——它们已经作为聊天消息进了聊天流，
再在状态里重复列一遍纯属浪费 token。
"""

from __future__ import annotations

from collections import deque
from typing import Deque, Tuple

from .render import LiveEvent

# 互动列表最多保留的条数，防止投毒式刷屏把快照撑爆
MAX_RECENT_EVENTS = 12


class LiveRoomStats:
    """直播间互动统计。

    版本号 ``revision`` 是**快照内容版本号**，只在快照内容真的会变时自增：
    记录一条互动事件，或者窗口内的事件刚过期（列表少了一条）。
    这样既不会因为别的原因（例如弹幕）白刷版本号，也不会漏掉「过期」这种内容变化。
    """

    def __init__(self, *, recent_window_seconds: float) -> None:
        """初始化统计。

        Args:
            recent_window_seconds: 互动事件在快照里保留的时长（秒）。
        """

        self._recent_window_seconds = max(float(recent_window_seconds), 1.0)
        self._recent: Deque[Tuple[float, LiveEvent]] = deque(maxlen=MAX_RECENT_EVENTS)
        self._revision = 0

    def record(self, event: LiveEvent, *, now: float) -> None:
        """记录一条互动事件（礼物 / 上舰 / 进场）。"""

        self._recent.append((now, event))
        self._revision += 1

    def take_revision(self, *, now: float) -> int:
        """取当前版本号，供变更检测使用；顺带清掉已过期的事件。

        过期本身会让快照内容变化，因此清理确实丢掉事件时同样自增版本号，
        否则「礼物滚出窗口」这件事永远不会被轮询发现，快照会一直挂着旧互动。
        """

        if self._trim_recent(now):
            self._revision += 1
        return self._revision

    def render(self, *, now: float, room_id: int, connection: str) -> str:
        """渲染一份中文状态快照（不含 ``<world_state>`` 包裹，由基座负责包裹）。"""

        self._trim_recent(now)

        lines = [f"直播间 {room_id}：{connection}"]

        if not self._recent:
            lines.append(f"最近 {int(self._recent_window_seconds)} 秒内没有礼物、上舰或进场。")
            return "\n".join(lines)

        lines.append(f"最近 {int(self._recent_window_seconds)} 秒内的互动（从早到晚）：")
        for _, event in self._recent:
            lines.append(f"- {event.text}")
        return "\n".join(lines)

    # ------------------------------------------------------------------ 内部实现

    def _trim_recent(self, now: float) -> bool:
        """丢弃互动窗口之外的事件；返回本次是否真的丢掉了事件。"""

        dropped = False
        deadline = now - self._recent_window_seconds
        while self._recent and self._recent[0][0] < deadline:
            self._recent.popleft()
            dropped = True
        return dropped
