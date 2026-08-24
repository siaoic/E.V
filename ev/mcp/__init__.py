"""MCP 模块包：管理器 / 工具注册表 / stdio 与 HTTP 传输。

用法：
    from ev.mcp.manager import MCPManager
    mcp = MCPManager({"enabled": True, "config_path": "..."})
    await mcp.initialize()
    tools = mcp.get_tools_for_llm()
"""

from ev.mcp.manager import MCPManager, get_mcp_dir
from ev.mcp.registry import MCPToolRegistry
from ev.mcp.llm_bridge import call_mcp_tool, get_mcp_tools_for_llm

__all__ = [
    "MCPManager", "MCPToolRegistry", "get_mcp_dir",
    "get_mcp_tools_for_llm", "call_mcp_tool",
]
