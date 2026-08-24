from __future__ import annotations
import asyncio
from typing import Any, Awaitable, Callable, Optional


def register(ctx) -> None:
    cfg = ctx.config
    impl_name = cfg.get("impl_name", "bilibili")
    room_id = cfg.get("room_id")      # int/str，缺 None
    sessdata = cfg.get("sessdata") or None

    from ev.kernel.slots import SlotName
    if ctx.slots is None:
        ctx.log("warn", "ctx.slots 为空，跳过 danmaku 注册")
        return
    impl = BilibiliDanmakuService(name=impl_name, room_id=room_id, sessdata=sessdata)
    try:
        ctx.slots.register(SlotName.danmaku, impl_name, impl)
    except Exception as e:
        ctx.log("error", f"注册 danmaku 槽位失败: {e}")
        return
    if room_id:
        ctx.log("ok", f"已注册 Danmaku(B站): {impl_name} 房间 {room_id}")
    else:
        ctx.log("ok", f"已注册 Danmaku(B站): {impl_name}（未配置 room_id，connect 时需手动指定）")


class BilibiliDanmakuService:
    """DanmakuContract 最小 stub（Bilibili 弹幕占位）。

    DanmakuContract 在 slots.py 是占位 Protocol（仅要求 name: str），
    这里加上 connect/disconnect/on_message 以及测试用的 _emit 辅助方法。
    """

    def __init__(self, name: str = "bilibili", room_id=None, sessdata=None) -> None:
        self.name = name
        self.room_id = room_id
        self.sessdata = sessdata
        self.running: bool = False
        self._handlers: list[Callable[[dict], Any]] = []

    async def connect(self) -> None:
        self.running = True
        # stub：不真实连接 ws

    async def disconnect(self) -> None:
        self.running = False

    def on_message(self, handler: Callable[[dict], Awaitable[None] | None]) -> None:
        self._handlers.append(handler)

    # 方便测试注入假消息（非 Protocol 强制）
    async def _emit(self, msg: dict) -> None:
        for h in list(self._handlers):
            try:
                r = h(msg)
                if hasattr(r, "__await__"):
                    await r
            except Exception:
                pass
