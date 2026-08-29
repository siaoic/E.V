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
    # 消费 delegation.db 队列中的长任务（复用 run_task 执行，带 MCP 工具）
    if cfg.AGENT_DELEGATE_BACKEND:
        from ev.agent.async_delegation import ensure_worker
        from ev.agent import run_task

        def _delegate_executor(job: dict) -> Any:
            def _progress(step: int, max_steps: int, action: dict,
                          observation: str) -> None:
                # 每步进展只进工具日志（dim 灰色行），不进对话/不播报
                console.dim(f"[委派后台] #{job.get('id')} 步骤 {step}/{max_steps}："
                            f"{(action or {}).get('name', '?')} → "
                            f"{(observation or '')[:100]}")
            return run_task(str(job.get("task") or ""), mcp=runtime.mcp,
                            progress_cb=_progress)

        async def _on_delegation_done(job_id: int, task: str,
                                      result: str) -> None:
            """任务终态回流：写黑板 + 主动播报（对齐进程内路径闭环）。

            进程内路径（bridge._run_and_report）执行完会播报"任务完成了"，
            持久化队列路径此前只落库不通知——用户对任务成败毫无感知。
            播报内容用 speakable_result 瘦身：只播 1 句结论，任务执行过程/
            文件路径等细节留在黑板与控制台工具日志，不读进 TTS；失败样式
            结果播报失败原因。
            """
            from ev.agent.blackboard import get_blackboard
            from ev.agent.bridge import notify_user
            from ev.agent.loop import is_failure_result, speakable_result
            # 结果写黑板（后续对话可召回，省一次 LLM 信息提取）
            try:
                await get_blackboard().put(
                    "delegation_result",
                    {"task": task, "result": (result or "")[:2000]},
                    source="delegation_worker",
                )
            except Exception:
                pass
            failed = is_failure_result(result)
            prefix = "后台任务没完成" if failed else "任务完成了"
            summary = speakable_result(result) or ("任务没完成" if failed else "办好了")
            try:
                await notify_user(runtime, f"{prefix}：{summary}")
            except Exception as e:
                console.warn(f"[委派后台] 结果播报失败：{e}")
            # 完整结果落工具日志（控制台可见，不进对话）：排查用
            console.dim(f"[委派后台] 任务 #{job_id} 完整结果：{(result or '')[:500]}")

        # 必须把主循环交给 worker：任务协程复用主循环创建的 MCP 客户端 /
        # output_lock 等 asyncio 原语，回主循环执行避免跨循环绑定错误
        ensure_worker(_delegate_executor, main_loop=asyncio.get_running_loop(),
                      on_done=_on_delegation_done)


async def teardown(runtime: "RuntimeContext") -> None:
    """Agent scheduler 无显式 stop；worker 为 daemon thread，随进程退出释放。"""
    return None
