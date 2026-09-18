"""世界事件总线：延迟投递 + 逐世界节流。

延迟投递用于防「先知穿帮」：OBS 把游戏画面延迟 N 秒播出，来自该世界的事件
也必须延迟 N 秒才允许进入 AI 上下文。节流则针对高频噪声（例如连续掉血），
避免同一世界的事件刷屏。

``throttle_seconds`` 以回调方式注入：配置热重载后无需重启总线即可生效。
"""

from __future__ import annotations

import asyncio
import heapq
import logging
import time
from collections import deque
from typing import Awaitable, Callable, Deque, Dict, List, Tuple

from .types import WorldEvent, WorldEventTrigger

# 最近事件环形缓冲长度（WebUI 世界状态页用；内存态，不落库）
HISTORY_LIMIT = 200


class WorldEventBus:
    """按投递语义把世界事件送到宿主的异步总线。"""

    def __init__(
        self,
        *,
        deliver: Callable[[WorldEvent], Awaitable[None]],
        throttle_seconds: Callable[[], float],
        logger: logging.Logger,
    ) -> None:
        """初始化事件总线。

        Args:
            deliver: 事件到期后的投递回调。
            throttle_seconds: 返回「同一世界两次 DEBOUNCE/PIGGYBACK 投递之间最小间隔（秒）」的回调。
            logger: 主程序日志器。
        """

        self._deliver = deliver
        self._throttle_seconds = throttle_seconds
        self._logger = logger
        self._pending: List[Tuple[float, int, WorldEvent]] = []
        self._sequence = 0
        self._last_delivered_at: Dict[str, float] = {}
        self._history: Deque[Dict[str, object]] = deque(maxlen=HISTORY_LIMIT)
        self._wakeup = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def publish(self, event: WorldEvent) -> None:
        """把一条事件排入总线。

        Args:
            event: 待投递的世界事件。
        """

        due_at = time.monotonic() + max(float(event.delay_seconds), 0.0)
        heapq.heappush(self._pending, (due_at, self._sequence, event))
        self._sequence += 1
        self._wakeup.set()

    def start(self) -> None:
        """启动投递循环；重复调用无副作用。"""

        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        """停止投递循环并丢弃未到期事件。"""

        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    @property
    def pending_count(self) -> int:
        """当前尚未到期的事件数量。"""

        return len(self._pending)

    @property
    def history(self) -> List[Dict[str, object]]:
        """最近事件记录副本（含被节流丢弃的），最早在前。"""

        return list(self._history)

    async def _run(self) -> None:
        """按最早到期时间逐个投递事件。"""

        while True:
            now = time.monotonic()
            while self._pending and self._pending[0][0] <= now:
                _, _, event = heapq.heappop(self._pending)
                await self._deliver_safely(event)
                now = time.monotonic()

            self._wakeup.clear()
            if not self._pending:
                await self._wakeup.wait()
                continue

            timeout = max(self._pending[0][0] - time.monotonic(), 0.0)
            try:
                await asyncio.wait_for(self._wakeup.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                pass

    async def _deliver_safely(self, event: WorldEvent) -> None:
        """投递单条事件；单个世界失败不得中断总线。"""

        if event.trigger in (WorldEventTrigger.DEBOUNCE, WorldEventTrigger.PIGGYBACK):
            throttle_seconds = max(float(self._throttle_seconds()), 0.0)
            now = time.monotonic()
            last_delivered_at = self._last_delivered_at.get(event.world_name, 0.0)
            if now - last_delivered_at < throttle_seconds:
                # 被节流丢弃的事件不需要补偿：同一世界的状态变化已由 dirty 标记承载。
                self._record(event, delivered=False, reason="节流丢弃")
                return
            self._last_delivered_at[event.world_name] = now

        try:
            await self._deliver(event)
        except Exception:  # noqa: BLE001 - 总线必须与单个世界的故障隔离
            # 不做静默兜底：完整打出异常，便于定位是哪个世界/哪种投递语义出问题。
            self._logger.exception(
                f"投递世界事件失败: world={event.world_name} trigger={event.trigger.value}"
            )
            self._record(event, delivered=False, reason="投递失败")
            return
        self._record(event, delivered=True, reason="")

    def _record(self, event: WorldEvent, *, delivered: bool, reason: str) -> None:
        """把一条事件写入最近历史（WebUI 展示用）。"""

        self._history.append(
            {
                "world": event.world_name,
                "event_type": event.event_type,
                "trigger": event.trigger.value,
                "text": event.text,
                "delivered": delivered,
                "reason": reason,
                "at": time.strftime("%H:%M:%S"),
            }
        )
