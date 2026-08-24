"""工具执行：并行执行 + 失败自动重试 + 单轮结果熔断。"""

import asyncio
import json
from typing import Any, List

from ev.utils import console
from ev.llm.utils.constants import _MAX_ROUND_TOOL_CHARS
from ev.llm.tools.formatter import _format_search_result, _format_tool_result


async def _execute_tool_calls(mcp, tool_calls: list) -> List[dict]:
    """并行执行工具并返回结果消息（对标 NagaAgent agentic_tool_loop：
    一轮多个工具 asyncio.gather 并行执行，显著缩短多工具轮次延迟）。

    熔断：单轮累计工具结果超过 _MAX_ROUND_TOOL_CHARS 时，后续工具结果
    直接截断为 0 并注入提示，避免 30 轮工具调用 × 8000 字符
    一次性进 LLM 上下文把免费档模型打爆（400/截断/超时）。
    """
    from plugins.builtin.tools import call_tool

    # 共享计数器：本轮已消耗的工具结果字符数
    state = {"round_chars": 0, "truncated": False}

    async def _run(tc: dict) -> dict:
        name = tc["function"]["name"]
        try:
            args = json.loads(tc["function"]["arguments"] or "{}")
        except (json.JSONDecodeError, TypeError):
            args = {}
        console.dim(f"  ↳ 执行「{name}」...")
        result = await _call_tool_with_retry(name, args, mcp)
        if isinstance(result, dict) and isinstance(result.get("results"), list):
            # 搜索类结果：逐条醒目展示（标题/链接/摘要），便于直播时直接读取
            console.accent(_format_search_result(name, result))
        else:
            console.dim(_format_tool_result(name, result))
        # 单轮累计熔断：本轮已超阈值时把后续结果直接截断为 0，
        # 模型依旧能看到「有工具被熔断」的提示
        if state["truncated"]:
            result = (f"[后续工具结果因本轮累计超过 "
                      f"{_MAX_ROUND_TOOL_CHARS // 1000}K 字符被截断]")
        else:
            if isinstance(result, str):
                state["round_chars"] += len(result)
            if state["round_chars"] > _MAX_ROUND_TOOL_CHARS:
                state["truncated"] = True
                result = (f"[后续工具结果因本轮累计超过 "
                          f"{_MAX_ROUND_TOOL_CHARS // 1000}K 字符被截断]")
        return {
            "role": "tool",
            "name": name,
            "tool_call_id": tc.get("id") or f"call_{name}",
            "content": result,
        }

    async def _call_tool_with_retry(name: str, args: dict, mcp) -> Any:
        """单工具执行失败自动重试 1 次（指数退避 1s），减少偶发失败。"""
        try:
            return await call_tool(name, args, mcp)
        except Exception as e:
            console.warn(f"  ↳ 「{name}」执行失败（{e}），1s 后自动重试...")
            await asyncio.sleep(1.0)
            return await call_tool(name, args, mcp)

    return await asyncio.gather(*(_run(tc) for tc in tool_calls))
