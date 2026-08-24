"""弹幕过滤器骨架：装饰 BilibiliService，注册 SlotName.danmaku → impl=bili-filtered。"""
from __future__ import annotations

import asyncio
from typing import Any, Callable, List, Optional


def register(ctx) -> None:
    cfg = ctx.config
    impl_name: str = cfg.get("impl_name", "bili-filtered")
    min_length: int = int(cfg.get("min_length", 2))
    blacklist: List[str] = list(cfg.get("blacklist", []) or [])
    allow_gifts: bool = bool(cfg.get("allow_gifts", True))

    impl = DanmakuFilterWrapper(
        name=impl_name,
        min_length=min_length,
        blacklist=blacklist,
        allow_gifts=allow_gifts,
    )

    try:
        from ev.kernel.slots import SlotName
    except Exception as e:
        ctx.log("error", f"无法导入 SlotName: {e}")
        return
    if ctx.slots is None:
        ctx.log("warn", "ctx.slots 为空，跳过 danmaku 注册")
        return
    try:
        ctx.slots.register(SlotName.danmaku, impl_name, impl)
    except Exception as e:
        ctx.log("error", f"注册 danmaku 槽位失败 ({impl_name}): {e}")
        return
    ctx.log(
        "ok",
        f"已注册 Danmaku(Filtered): {impl_name} min_len={min_length} blacklist={blacklist}",
    )


class DanmakuFilterWrapper:
    """DanmakuContract 装饰器 stub：过滤黑名单/过短消息。"""

    def __init__(
        self,
        name: str = "bili-filtered",
        min_length: int = 2,
        blacklist: Optional[List[str]] = None,
        allow_gifts: bool = True,
    ) -> None:
        self.name = name
        self.min_length = min_length
        self.blacklist: List[str] = list(blacklist) if blacklist else []
        self.allow_gifts = allow_gifts
        self.running: bool = False
        self._handlers: List[Callable[[dict], Any]] = []
        self._inner: Optional[Any] = None  # 被装饰的 BilibiliService（stub 留空）

    async def connect(self) -> None:
        self.running = True
        if self._inner is not None and hasattr(self._inner, "connect"):
            try:
                r = self._inner.connect()
                if asyncio.iscoroutine(r):
                    await r
            except Exception:
                pass

    async def disconnect(self) -> None:
        self.running = False
        if self._inner is not None and hasattr(self._inner, "disconnect"):
            try:
                r = self._inner.disconnect()
                if asyncio.iscoroutine(r):
                    await r
            except Exception:
                pass

    def on_message(self, handler: Callable[[dict], Any]) -> None:
        self._handlers.append(handler)

    # ---- 过滤器核心 ----
    def _allow(self, msg: dict) -> bool:
        mtype = str(msg.get("type") or "danmaku")
        if mtype in ("gift", "superchat"):
            return self.allow_gifts
        text = str(msg.get("text") or msg.get("content") or "")
        if len(text) < self.min_length:
            return False
        for w in self.blacklist:
            if w and w in text:
                return False
        return True

    async def _emit(self, msg: dict) -> None:
        if not self._allow(msg):
            return
        for h in list(self._handlers):
            try:
                r = h(msg)
                if asyncio.iscoroutine(r):
                    await r
            except Exception:
                pass
