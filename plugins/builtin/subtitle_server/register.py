"""字幕服务器骨架（SSE 推流）：注册 SlotName.ui → impl=subtitle-sse。"""
from __future__ import annotations

from typing import Callable, Optional


def register(ctx) -> None:
    cfg = ctx.config
    impl_name: str = cfg.get("impl_name", "subtitle-sse")
    port: int = int(cfg.get("port", 7860))
    route: str = cfg.get("route", "/subtitle")

    impl = SubtitleSSE(name=impl_name, port=port, route=route)

    try:
        from ev.kernel.slots import SlotName
    except Exception as e:
        ctx.log("error", f"无法导入 SlotName: {e}")
        return
    if ctx.slots is None:
        ctx.log("warn", "ctx.slots 为空，跳过 ui 注册")
        return
    try:
        ctx.slots.register(SlotName.ui, impl_name, impl)
    except Exception as e:
        ctx.log("error", f"注册 ui 槽位失败 ({impl_name}): {e}")
        return
    ctx.log("ok", f"已注册 UI(SubtitleSSE): {impl_name} port={port} route={route!r}")


class SubtitleSSE:
    """SubtitleServer SSE 占位 stub：name 满足 UiContract Protocol。"""

    def __init__(
        self,
        name: str = "subtitle-sse",
        port: int = 7860,
        route: str = "/subtitle",
    ) -> None:
        self.name = name
        self.port = port
        self.route = route
        self.running: bool = False
        self._subtitle_cb: Optional[Callable] = None
        self._queue: list[str] = []

    async def start(self) -> None:
        self.running = True

    async def stop(self) -> None:
        self.running = False

    def push(self, text: str) -> None:
        self._queue.append(text)
        if self._subtitle_cb is not None:
            try:
                self._subtitle_cb(text)
            except Exception:
                pass

    def set_subtitle_callback(self, cb: Optional[Callable]) -> None:
        self._subtitle_cb = cb

    def drain(self) -> list[str]:
        q = self._queue
        self._queue = []
        return q
