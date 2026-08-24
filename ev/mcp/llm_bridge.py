"""MCP 与 LLM 工具系统的桥接：MCP 工具的合并 / 调用编排集中在此。

消费方（src/llm/tools/__init__.py）只负责本地工具，涉及 MCP 的分支统一走
本模块，避免 MCP 逻辑散落在本地工具模块里。所有对外接口不变：
  - get_mcp_tools_for_llm(mcp)  取 MCP 工具（OpenAI Function Calling 格式）
  - call_mcp_tool(name, args, mcp)  MCP 优先调用，未命中返回 None（本地兜底）
"""

from __future__ import annotations

import asyncio
import json
from typing import List, Optional


def get_mcp_tools_for_llm(mcp) -> List[dict]:
    """取 MCP 服务器提供的工具列表（OpenAI Function Calling 格式）。

    mcp 为 None 或已禁用 / 无工具时返回空列表（与调用方合并逻辑兼容）。
    """
    if mcp is None:
        return []
    return mcp.get_tools_for_llm() or []


def describe_mcp_servers(mcp) -> str:
    """生成 MCP 服务器能力说明（供注入 system prompt 告知模型）。

    逐台列出已成功启动、注册了工具的服务器：服务器名 + 配置里的
    description + 该服务器注册的工具名，让模型知道有哪些联网能力可用
    （避免有工具却不调用、道歉"无法搜索"）。无可用服务器时返回空字符串。
    """
    if mcp is None or not mcp.is_enabled or not mcp.mcp_servers:
        return ""
    lines = []
    for name, srv in mcp.mcp_servers.items():
        tool_names = [t["name"] for t in mcp.tool_registry.get_tools_by_server(name)]
        if not tool_names:
            continue  # 启动失败/无工具注册的服务器不告知模型
        desc = (srv.get("description") or "").strip()
        head = f"- {name}"
        if desc:
            head += f"：{desc}"
        head += f"（工具：{', '.join(tool_names)}）"
        lines.append(head)
    return "\n".join(lines)


async def call_mcp_tool(name: str, args: dict, mcp) -> Optional[str]:
    """MCP 优先调用单个工具。

    返回该工具的执行结果文本；mcp 未启用 / 非 MCP 工具 / 无结果时返回 None，
    交由调用方走本地工具兜底。

    线程安全：MCP 客户端生命周期绑定创建它的主事件循环。若当前调用发生在
    其他线程的事件循环（如 Agent 委派 worker 线程 asyncio.run），桥回主循环
    执行，避免跨循环操作 anyio 流报错；主循环内调用零开销直走。
    """
    if mcp is None or not mcp.is_enabled:
        return None

    async def _do_call() -> Optional[str]:
        results = await mcp.handle_tool_calls(
            [{"id": f"local_{name}", "type": "function",
              "function": {"name": name, "arguments": json.dumps(args or {}, ensure_ascii=False)}}]
        )
        if results:
            return results[0]["content"]
        return None

    loop = getattr(mcp, "_loop", None)
    if loop is not None and asyncio.get_running_loop() is not loop:
        # 跨线程：桥回主循环执行，阻塞等待结果（超时兜底防主循环卡死拖垮 worker）
        try:
            future = asyncio.run_coroutine_threadsafe(_do_call(), loop)
            return future.result(timeout=60.0)
        except Exception as e:
            return f"工具调用失败（跨线程调度超时/异常）：{e}"
    return await _do_call()
