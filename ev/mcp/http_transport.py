"""MCP HTTP 传输层 —— 基于官方 mcp SDK（mcp.client.streamable_http）。

职责：连接远程 streamable HTTP MCP 服务器（如在线 API 网关）。
相比早期自研的差异：SSE / 202 异步模式 / 会话恢复等协议细节由官方 SDK 处理，
自定义请求头（如认证）通过 httpx2.AsyncClient 注入。
"""

from __future__ import annotations

from ev.utils import console
from ev.kernel.exceptions import EVBaseException, ErrorCode
from ev.mcp._base import MCPClientTransportBase
from ev.mcp.registry import MCPToolRegistry


class MCPHttpTransport(MCPClientTransportBase):
    """MCP HTTP（streamable）传输：官方 SDK 连接远程服务器。"""

    tool_type = "mcp_http"

    def _create_client(self):
        url = self.config.get("url", "")
        if not url:
            raise EVBaseException(
                ErrorCode.MCP_SERVER_FAILED,
                f"MCP 服务器 {self.server_name} 缺少 url")

        console.info(f"🌐 连接 MCP HTTP 服务器: {self.server_name} -> {url}")

        from mcp import Client
        from mcp.client.streamable_http import streamable_http_client

        # 自定义请求头（认证等）通过 http_client 注入
        headers = self.config.get("headers") or {}
        if headers:
            import httpx2
            transport = streamable_http_client(
                url, http_client=httpx2.AsyncClient(headers=headers),
            )
        else:
            transport = streamable_http_client(url)
        return Client(transport, read_timeout_seconds=self.timeout)

    def _ready_log(self, tool_count: int) -> None:
        console.ok(f"MCP HTTP 服务器 {self.server_name} 就绪，{tool_count} 个工具")

    def _stop_log(self) -> None:
        console.dim(f"🛑 MCP HTTP 服务器 {self.server_name} 已断开")
