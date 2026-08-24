"""LLMBrain 创建 + warmup + 记忆 manager 提示（L376-384）。"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ev.kernel.runtime import RuntimeContext


async def setup(runtime: "RuntimeContext") -> None:
    """LLMBrain 创建 + warmup + 记忆系统提示打印。"""
    from ev.utils import console
    from ev.llm.llm_brain import LLMBrain
    from tools.memory import memory

    cfg = runtime.cfg
    runtime.brain = LLMBrain(mcp=runtime.mcp)

    # 连接预热：启动空闲期发一个最小请求，把 TLS 握手冷启动挪到后台，
    # 用户第一次提问即命中热连接（首轮 TTFT 实测 2565ms → 热连接 ~1s）
    asyncio.create_task(runtime.brain.warmup())

    if cfg.MEMORY_ENABLED:
        console.dim(f"记忆系统：已启用（{memory.count()} 个记忆文件）")


async def teardown(runtime: "RuntimeContext") -> None:
    """LLM brain 无显式 stop；由进程结束释放 client 连接池。"""
    return None
