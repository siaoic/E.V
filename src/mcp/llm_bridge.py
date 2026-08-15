"""MCP 与 LLM 工具系统的桥接：MCP 工具的合并 / 调用编排集中在此。

消费方（src/llm/tools/__init__.py）只负责本地工具，涉及 MCP 的分支统一走
本模块，避免 MCP 逻辑散落在本地工具模块里。所有对外接口不变：
  - get_mcp_tools_for_llm(mcp)  取 MCP 工具（OpenAI Function Calling 格式）
  - call_mcp_tool(name, args, mcp)  MCP 优先调用，未命中返回 None（本地兜底）
"""

from __future__ import annotations

import json
from typing import List, Optional


def get_mcp_tools_for_llm(mcp) -> List[dict]:
    """取 MCP 服务器提供的工具列表（OpenAI Function Calling 格式）。

    mcp 为 None 或已禁用 / 无工具时返回空列表（与调用方合并逻辑兼容）。
    """
    if mcp is None:
        return []
    return mcp.get_tools_for_llm() or []


async def call_mcp_tool(name: str, args: dict, mcp) -> Optional[str]:
    """MCP 优先调用单个工具。

    返回该工具的执行结果文本；mcp 未启用 / 非 MCP 工具 / 无结果时返回 None，
    交由调用方走本地工具兜底。
    """
    if mcp is None or not mcp.is_enabled:
        return None
    results = await mcp.handle_tool_calls(
        [{"id": f"local_{name}", "type": "function",
          "function": {"name": name, "arguments": json.dumps(args or {}, ensure_ascii=False)}}]
    )
    if results:
        return results[0]["content"]
    return None
