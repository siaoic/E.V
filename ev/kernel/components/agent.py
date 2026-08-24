"""AgentScheduler + delegation worker（L426-442）。"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ev.kernel.runtime import RuntimeContext


async def setup(runtime: "RuntimeContext") -> None:
    """Agent 定时任务调度 + 后台委派 worker。"""
    from ev.utils import console

    cfg = runtime.cfg

    # Agent 定时任务调度：加载清单 + 后台循环到点触发（!agent_schedule 管理）
    from ev.agent.scheduler import AgentScheduler
    runtime.agent_scheduler = AgentScheduler()
    runtime.agent_scheduler.load()
    if cfg.AGENT_ENABLED:
        asyncio.create_task(runtime._agent_schedule_loop())

    # 后台委派 worker（3.8）：AGENT_DELEGATE_BACKEND 开启时启动常驻线程，
    # 消费 delegation.db 队列中的长任务（复用 run_task 执行）
    if cfg.AGENT_DELEGATE_BACKEND:
        from ev.agent.async_delegation import ensure_worker
        from ev.agent import run_task

        def _delegate_executor(job: dict) -> Any:
            return run_task(str(job.get("task") or ""))

        ensure_worker(_delegate_executor)


async def teardown(runtime: "RuntimeContext") -> None:
    """Agent scheduler 无显式 stop；worker 为 daemon thread，随进程退出释放。"""
    return None
