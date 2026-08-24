"""EvolutionEngine 创建 + evolution_periodic_loop 启动（L251-252 + L422-424）。"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ev.kernel.runtime import RuntimeContext


async def setup(runtime: "RuntimeContext") -> None:
    """EvolutionEngine 创建 + 周期循环启动。"""
    from ev.llm.evolution import EvolutionEngine

    cfg = runtime.cfg
    # 自我进化引擎（对话后后台复盘，随配置开关创建）（原 L251-252）
    runtime.evolution = EvolutionEngine() if cfg.EVOLUTION_ENABLED else None

    # 自我进化：定期自我提示（空闲期主动补复盘，后台循环）（原 L422-424）
    if runtime.evolution is not None:
        asyncio.create_task(runtime._evolution_periodic_loop())


async def teardown(runtime: "RuntimeContext") -> None:
    """Evolution engine 无显式清理；由 asyncio tasks cancel/进程结束释放。"""
    return None
