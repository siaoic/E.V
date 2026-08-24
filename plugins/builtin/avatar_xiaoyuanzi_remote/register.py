from __future__ import annotations
import asyncio
from typing import Any, AsyncIterator, Callable, Dict, Optional


def register(ctx) -> None:
    # 配置：endpoint（远程服务地址）、impl_name（缺省 xiaoyuanzi）
    cfg = ctx.config
    endpoint = cfg.get("endpoint", "http://127.0.0.1:8765")
    impl_name = cfg.get("impl_name", "xiaoyuanzi")

    from ev.kernel.slots import SlotName
    if ctx.slots is None:
        ctx.log("warn", "ctx.slots 为空，跳过 avatar 注册")
        return
    impl = XiaoyuanziAvatar(endpoint=endpoint, name=impl_name)
    try:
        ctx.slots.register(SlotName.avatar, impl_name, impl)
    except Exception as e:
        ctx.log("error", f"注册 avatar 槽位失败: {e}")
        return
    ctx.log("ok", f"已注册 Avatar: {impl_name} (endpoint={endpoint})")


class XiaoyuanziAvatar:
    """AvatarContract 最小 stub 实现（小院子远程版占位）。

    同时满足 slots.py AvatarContract Protocol（connect/ensure_connected/close 等）
    和骨架生命周期语义（start/stop/wait_fully_ready/send_text/set_emotion）。
    """

    def __init__(self, endpoint: str, name: str = "xiaoyuanzi") -> None:
        self.name = name
        self.endpoint = endpoint
        self.running: bool = False
        self.is_connected: bool = False
        self._emotion: str = "idle"
        self._event_handlers: Dict[str, list[Callable]] = {}

    # ---- 骨架生命周期（任务描述风格） ----
    async def start(self) -> None:
        self.running = True
        # stub：不真正建立 websocket

    async def stop(self) -> None:
        self.running = False
        self.is_connected = False

    async def wait_fully_ready(self, timeout: float = 30.0) -> bool:
        # stub：假装立即 ready
        self.is_connected = True
        return True

    async def send_text(self, text: str) -> None:
        # stub：不真实发送
        pass

    async def set_emotion(self, emotion: str, duration: float = 2.0) -> None:
        self._emotion = emotion
        await asyncio.sleep(0)  # async marker

    # ---- AvatarContract Protocol 方法 ----
    async def connect(self) -> None:
        """AvatarContract.connect → 等价于 start + wait_fully_ready。"""
        await self.start()
        await self.wait_fully_ready()

    async def ensure_connected(self) -> None:
        """AvatarContract.ensure_connected → 确保连接建立。"""
        if not self.is_connected:
            await self.wait_fully_ready()

    async def close(self) -> None:
        """AvatarContract.close → 等价于 stop。"""
        await self.stop()

    def on_event(self, event_name: str, handler: Callable) -> None:
        """AvatarContract.on_event → 注册事件处理器（stub：仅记录）。"""
        self._event_handlers.setdefault(event_name, [])
        if handler not in self._event_handlers[event_name]:
            self._event_handlers[event_name].append(handler)

    async def subscribe_event(self, event_name: str) -> bool:
        """AvatarContract.subscribe_event → stub，始终返回 True。"""
        await asyncio.sleep(0)
        return True

    async def inject_parameters(self, params: Dict[str, float]) -> None:
        """AvatarContract.inject_parameters → stub，不做任何事。"""
        await asyncio.sleep(0)

    async def trigger_motion(self, motion_file: str) -> bool:
        """AvatarContract.trigger_motion → stub，始终返回 True。"""
        await asyncio.sleep(0)
        return True

    async def trigger_hotkey(self, hotkey_id: str, priority: str = "High") -> None:
        """AvatarContract.trigger_hotkey → stub。"""
        await asyncio.sleep(0)

    async def activate_expression(self, expr_file: str, active: bool = True) -> bool:
        """AvatarContract.activate_expression → stub，始终返回 True。"""
        await asyncio.sleep(0)
        return True
