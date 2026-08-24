"""ButlerAgent + 记忆系统（L245-250 前半 + L352-374 + drain）。"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ev.kernel.runtime import RuntimeContext

# 保留 memory 单例：旧代码 from tools.memory import memory 依赖此单例
from tools.memory import memory  # noqa: F401


async def setup(runtime: "RuntimeContext") -> None:
    """ButlerAgent 创建 + 记忆 manager 创建 + 预热 drain。"""
    from ev.utils import console
    from ev.llm.butler_agent import ButlerAgent

    cfg = runtime.cfg

    # ButlerAgent 记忆管家（原 L245-250 前半）
    if cfg.MEMORY_ENABLED:
        runtime.butler = ButlerAgent()
    else:
        runtime.butler = None

    # 初始化记忆系统（原 L352-374）
    runtime.mm = memory.get_manager()
    runtime.mm.load()
    runtime.mm.new_session()
    if cfg.MEMORY_ENABLED:
        # 上次运行时 remember/forget 失败队列（drain 期间要等 memory service
        # 就绪，所以放后台任务里）
        async def _drain_retry_after_load() -> None:
            from plugins.builtin.tools.remember_fact import memory_tools
            # 等几帧让 mm.load() 完成 service 初始化
            await asyncio.sleep(0.5)
            try:
                n = await asyncio.to_thread(memory_tools.drain_retry_queue)
                if n:
                    console.dim(f"[记忆] 启动重放成功 {n} 条暂存记忆")
            except Exception as e:
                console.dim(f"[记忆] 重放失败队列失败：{e}")
        asyncio.create_task(_drain_retry_after_load())
        asyncio.create_task(memory.warmup())
        # 记忆时间衰减：后台定时清理长期未更新的非固定记忆
        asyncio.create_task(memory.decay_loop())
        # AI 自动整合蒸馏：碎片过多时后台蒸馏合并并删除旧条目
        asyncio.create_task(runtime._memory_integration_loop())


async def teardown(runtime: "RuntimeContext") -> None:
    """记忆系统无显式清理；memory 单例进程结束时自动释放。"""
    return None
