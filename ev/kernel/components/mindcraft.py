"""MindcraftBridge 创建 + _mindcraft_loop 启动（L335-344）。"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ev.kernel.runtime import RuntimeContext


async def setup(runtime: "RuntimeContext") -> None:
    """MindcraftBridge 创建 + _mindcraft_loop 后台任务启动。"""
    from ev.mindcraft.bridge import MindcraftBridge

    cfg = runtime.cfg
    # Mindcraft 双向桥（socket.io 连接 MindServer）：开关开启才创建，
    # 由后台循环负责连接/重连（引擎可能晚于主程序启动）。
    runtime.mindcraft_bridge = None
    if cfg.MINDCRAFT_BRIDGE_ENABLED:
        runtime.mindcraft_bridge = MindcraftBridge(
            server_url=f"http://127.0.0.1:{cfg.MINDCRAFT_MINDSERVER_PORT}",
            agent_name=cfg.MINDCRAFT_BOT_NAME,
            on_bot_output=runtime._on_mindcraft_bot_output,
        )
        asyncio.create_task(runtime._mindcraft_loop())


async def teardown(runtime: "RuntimeContext") -> None:
    """原 teardown 中 MindcraftBridge disconnect 段。"""
    import asyncio

    if runtime.mindcraft_bridge is not None:
        try:
            await asyncio.wait_for(
                runtime.mindcraft_bridge.disconnect(), timeout=5)
        except Exception:
            pass
