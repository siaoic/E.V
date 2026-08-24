"""工具执行入口：并行执行 + 转发 L3-A 三段式管线。

执行实现已迁移至 ev/agent/tool_pipeline.py（pre-execute / execute /
post-execute，含插件拦截、schema 校验、预算 stub、超时与后台重试、熔断）。
本模块保留既有入口签名与返回格式不变，行为 100% 一致。
"""

import asyncio
from typing import List


async def _execute_tool_calls(mcp, tool_calls: list) -> List[dict]:
    """并行执行工具并返回结果消息（对标 NagaAgent agentic_tool_loop：
    一轮多个工具 asyncio.gather 并行执行，显著缩短多工具轮次延迟）。

    熔断：单轮累计工具结果超过 _MAX_ROUND_TOOL_CHARS 时，后续工具结果
    直接截断为 0 并注入提示，避免 30 轮工具调用 × 8000 字符
    一次性进 LLM 上下文把免费档模型打爆（400/截断/超时）。
    """
    # 共享计数器：本轮已消耗的工具结果字符数
    state = {"round_chars": 0, "truncated": False}
    return await asyncio.gather(
        *(_execute_tool_call(mcp, tc, state) for tc in tool_calls))


async def _execute_tool_call(mcp, tc: dict, state: dict) -> dict:
    """执行单个工具（转发 L3-A 三段式管线）。

    state 为共享熔断计数（{"round_chars", "truncated"}）。独立可复用：
    L2-A 流式期间提前启动工具时逐工具调用，与 _execute_tool_calls 行为一致。
    """
    from ev.agent.tool_pipeline import tool_pipeline
    return await tool_pipeline.execute(mcp, tc, state)
