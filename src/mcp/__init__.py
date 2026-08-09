"""MCP 模块包：管理器 / 工具注册表 / stdio 与 HTTP 传输。

用法：
    from src.mcp.manager import MCPManager
    mcp = MCPManager({"enabled": True, "config_path": "..."})
    await mcp.initialize()
    tools = mcp.get_tools_for_llm()
"""

from src.mcp.manager import MCPManager, get_mcp_dir
from src.mcp.registry import MCPToolRegistry

__all__ = ["MCPManager", "MCPToolRegistry", "get_mcp_dir"]
