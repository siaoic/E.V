"""小院子本地化 Avatar 骨架：满足 AvatarContract，本地 exe + WebSocket 占位。"""
from __future__ import annotations

import asyncio
from typing import Any, Callable, Dict, List, Optional


def register(ctx) -> None:
    cfg = ctx.config
    impl_name: str = cfg.get("impl_name", "xiaoyuanzi-local")
    host: str = cfg.get("host", "127.0.0.1")
    port: int = int(cfg.get("port", 8765))
    exe_path: str = cfg.get("exe_path", "")

    impl = XiaoyuanziLocalAvatar(
        name=impl_name,
        host=host,
        port=port,
        exe_path=exe_path,
    )

    try:
        from ev.kernel.slots import SlotName
    except Exception as e:
        ctx.log("error", f"无法导入 SlotName: {e}")
        return
    if ctx.slots is None:
        ctx.log("warn", "ctx.slots 为空，跳过 avatar 注册")
        return
    try:
        ctx.slots.register(SlotName.avatar, impl_name, impl)
    except Exception as e:
        ctx.log("error", f"注册 avatar 槽位失败 ({impl_name}): {e}")
        return
    ctx.log(
        "ok",
        f"已注册 Avatar(xiaoyuanzi-local): {impl_name} {host}:{port} exe={exe_path!r}",
    )


class XiaoyuanziLocalAvatar:
    """AvatarContract 严格 stub：方法签名对齐 slots.py AvatarContract。"""

    def __init__(
        self,
        name: str = "xiaoyuanzi-local",
        host: str = "127.0.0.1",
        port: int = 8765,
        exe_path: str = "",
    ) -> None:
        self.name = name
        self.host = host
        self.port = port
        self.exe_path = exe_path
        self.is_connected: bool = False
        self._event_handlers: Dict[str, List[Callable]] = {}
        self._params: Dict[str, float] = {}
        self._expr_active: Dict[str, bool] = {}

    # ---- lifecycle（额外，非 Protocol 强制）----
    async def start(self) -> None:
        # stub：不启动真实 exe / ws
        self.is_connected = True

    async def stop(self) -> None:
        self.is_connected = False

    # ---- AvatarContract Protocol 方法 ----
    async def connect(self) -> None:
        await self.start()

    async def ensure_connected(self) -> None:
        if not self.is_connected:
            await self.connect()

    async def close(self) -> None:
        await self.stop()

    def on_event(self, event_name: str, handler: Callable) -> None:
        self._event_handlers.setdefault(event_name, [])
        if handler not in self._event_handlers[event_name]:
            self._event_handlers[event_name].append(handler)

    async def subscribe_event(self, event_name: str) -> bool:
        await asyncio.sleep(0)
        return True

    async def inject_parameters(self, params: Dict[str, float]) -> None:
        self._params.update(params or {})
        await asyncio.sleep(0)

    async def trigger_motion(self, motion_file: str) -> bool:
        await asyncio.sleep(0)
        return True

    async def trigger_hotkey(self, hotkey_id: str, priority: str = "High") -> None:
        await asyncio.sleep(0)

    async def activate_expression(self, expr_file: str, active: bool = True) -> bool:
        self._expr_active[expr_file] = active
        await asyncio.sleep(0)
        return True

