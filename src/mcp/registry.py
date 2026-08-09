"""MCP 工具注册表 —— 严格参考 live-2d(2) 的 mcp-tool-registry.js。

职责：
  - 管理所有 MCP 服务器注册上来的工具
  - 按名称查找 / 按服务器过滤
  - 转换为 OpenAI Function Calling 格式（供 LLM tools 参数使用）
"""

from __future__ import annotations

from typing import Dict, List, Optional


class MCPToolRegistry:
    """MCP 工具注册表：注册 / 查找 / 格式转换。"""

    def __init__(self) -> None:
        # 每项: {name, description, parameters, server, type}
        self.tools: List[dict] = []

    # ------------------------------------------------------------------
    # 注册
    # ------------------------------------------------------------------

    def register_tools(self, server_name: str, tools: list, transport_type: str) -> None:
        """注册某服务器提供的工具列表。

        tools 形如 [{name, description, inputSchema}]（MCP tools/list 结果）。
        """
        for tool in tools:
            self.tools.append({
                "name": tool.get("name"),
                "description": tool.get("description", ""),
                "parameters": tool.get("inputSchema") or tool.get("parameters") or {"type": "object"},
                "server": server_name,
                "type": transport_type,   # 'mcp'（stdio）或 'mcp_http'
            })
            print(f"  ✅ 注册 {transport_type} 工具: {tool.get('name')}")

    # ------------------------------------------------------------------
    # 查找
    # ------------------------------------------------------------------

    def find_tool(self, tool_name: str) -> Optional[dict]:
        return next(
            (t for t in self.tools if t["name"] == tool_name
             and t["type"] in ("mcp", "mcp_http")),
            None,
        )

    def get_tools_by_server(self, server_name: str) -> List[dict]:
        return [t for t in self.tools if t["server"] == server_name]

    def get_tools_by_type(self, transport_type: str) -> List[dict]:
        return [t for t in self.tools if t["type"] == transport_type]

    def get_all_mcp_tools(self) -> List[dict]:
        return [t for t in self.tools if t["type"] in ("mcp", "mcp_http")]

    def get_all_tools(self) -> List[dict]:
        return list(self.tools)

    def has_tool(self, tool_name: str) -> bool:
        return self.find_tool(tool_name) is not None

    def is_mcp_tool(self, tool_name: str) -> bool:
        return any(
            t["name"] == tool_name and t["type"] in ("mcp", "mcp_http")
            for t in self.tools
        )

    def get_tool_count(self) -> int:
        return len(self.get_all_mcp_tools())

    def get_tool_names(self) -> List[str]:
        return [t["name"] for t in self.get_all_mcp_tools()]

    # ------------------------------------------------------------------
    # 格式转换（→ OpenAI Function Calling）
    # ------------------------------------------------------------------

    def to_openai_format(self) -> List[dict]:
        """转换为 OpenAI tools 参数格式。

        形如 [{"type": "function", "function": {name, description, parameters}}]
        """
        mcp_tools = self.get_all_mcp_tools()
        if not mcp_tools:
            return []
        return [
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t["description"],
                    "parameters": t["parameters"],
                },
            }
            for t in mcp_tools
        ]

    # ------------------------------------------------------------------
    # 统计 / 清理
    # ------------------------------------------------------------------

    def get_stats(self) -> dict:
        mcp_tools = self.get_all_mcp_tools()
        return {
            "total": len(mcp_tools),
            "by_type": {
                "stdio": len(self.get_tools_by_type("mcp")),
                "http": len(self.get_tools_by_type("mcp_http")),
            },
            "tool_names": [t["name"] for t in mcp_tools],
        }

    def clear(self) -> None:
        self.tools = []
