"""MCP HTTP 传输层 —— 严格参考 live-2d(2) 的 mcp-http-transport.js。

用 httpx 手写 MCP streamable HTTP 协议（无状态模式），不依赖官方 mcp SDK：
  - 每个请求独立 POST（带 Mcp-Session-Id，若有）
  - 响应可能是纯 JSON，也可能是 SSE（text/event-stream）——统一解析
  - 完全控制环境变量与超时，避免 SDK 兼容问题
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import List, Optional

from src.utils import console
from src.mcp.registry import MCPToolRegistry

try:
    import httpx
except ImportError:  # pragma: no cover
    httpx = None

_ACCEPT_HEADER = "application/json, text/event-stream"


class MCPHttpTransport:
    """MCP HTTP（streamable）传输。"""

    def __init__(
        self,
        server_config: dict,
        tool_registry: MCPToolRegistry,
        timeout: float = 30.0,
    ) -> None:
        self.config = server_config
        self.tool_registry = tool_registry
        self.timeout = timeout
        self.url = server_config.get("url", "")
        self.headers: dict = dict(server_config.get("headers") or {})
        self._session_id: Optional[str] = None
        self._client: Optional[httpx.AsyncClient] = None
        self.server_name: str = ""

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def start(self, server_name: str) -> None:
        """连接 HTTP 服务器并完成 initialize + tools/list 握手。"""
        if httpx is None:
            raise RuntimeError("未安装 httpx，无法使用 MCP HTTP 传输")

        self.server_name = server_name
        if not self.url:
            raise RuntimeError(f"MCP 服务器 {server_name} 缺少 url")

        console.info(f"🌐 连接 MCP HTTP 服务器: {server_name} -> {self.url}")

        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout),
            follow_redirects=True,
        )

        # 1) initialize（容错：偶发 202/失败不阻塞工具拉取，Tavily 无状态可直接 tools/list）
        try:
            init_resp = await self._request(
                "initialize",
                params={
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "vtuber-mcp-client", "version": "1.0.0"},
                },
            )
            if init_resp.get("error"):
                console.warn(f"⚠️ MCP 服务器 {server_name} initialize 异常: {init_resp['error']}")
        except Exception as e:
            console.warn(f"⚠️ MCP 服务器 {server_name} initialize 失败（继续拉取工具）: {e}")

        # 2) tools/list（失败不阻塞，仅警告）
        try:
            tools_resp = await self._request("tools/list", params={})
            if tools_resp.get("error"):
                raise RuntimeError(tools_resp["error"])
            server_tools: List[dict] = (tools_resp.get("result") or {}).get("tools", []) or []
            self.tool_registry.register_tools(server_name, server_tools, "mcp_http")
            console.ok(f"MCP HTTP 服务器 {server_name} 就绪，{len(server_tools)} 个工具")
        except Exception as e:
            console.warn(f"⚠️ MCP HTTP 服务器 {server_name} 获取工具列表失败: {e}")

    async def call_tool(self, tool_name: str, args: dict) -> str:
        """调用工具，返回可直接朗读的文本结果。"""
        resp = await self._request(
            "tools/call",
            params={"name": tool_name, "arguments": args},
        )
        if resp.get("error"):
            raise RuntimeError(f"工具 {tool_name} 调用失败: {resp['error'].get('message', resp['error'])}")

        result = resp.get("result") or {}
        content = result.get("content") or []
        text_parts = [c.get("text", "") for c in content if c.get("type") == "text" and c.get("text")]
        if text_parts:
            return "\n".join(text_parts)
        is_error = result.get("isError")
        fallback = json.dumps(result, ensure_ascii=False)[:2000]
        return f"工具返回了 {'错误' if is_error else '结果'}: {fallback}"

    async def stop(self) -> None:
        """关闭 HTTP 连接。"""
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:
                pass
            self._client = None
        console.dim(f"🛑 MCP HTTP 服务器 {self.server_name} 已断开")

    # ------------------------------------------------------------------
    # JSON-RPC over streamable HTTP
    # ------------------------------------------------------------------

    async def _request(self, method: str, params: dict, _attempt: int = 0) -> dict:
        """发送 JSON-RPC 请求。返回响应字典（含 id / result / error）。

        处理 streamable HTTP 的异步 202 模式：Tavily 等服务在繁忙时会先返回
        202 + 空 body，表示请求已接受——递增间隔重试（最多 3 次）取真实结果。
        """
        if self._client is None:
            raise RuntimeError(f"MCP HTTP 服务器未连接: {self.server_name}")

        headers = {
            "Accept": _ACCEPT_HEADER,
            "Content-Type": "application/json",
            **(self.headers or {}),
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id

        # 必须带唯一 id：无 id 的 JSON-RPC 消息会被 Tavily 等服务器当作
        # notification 处理（返回 202 且无响应体），无法拿到结果。
        req_id = f"{method}_{self.server_name}_{uuid.uuid4().hex[:8]}"
        body = json.dumps({
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
            "params": params,
        })

        try:
            response = await self._client.post(self.url, content=body, headers=headers)
        except httpx.HTTPError as e:
            raise RuntimeError(f"HTTP 请求失败: {e}")

        # 记录 session id（若服务器返回）
        sid = response.headers.get("Mcp-Session-Id")
        if sid:
            self._session_id = sid

        # 202 Accepted + 空 body：异步接受，递增间隔重试取结果
        if response.status_code == 202 and not response.content:
            if _attempt < 2:
                await asyncio.sleep(0.6 * (2 ** _attempt))
                return await self._request(method, params, _attempt + 1)
            raise RuntimeError(
                f"MCP HTTP 服务器返回 202 但未提供响应（{self.server_name}，method={method}）"
            )

        if response.status_code >= 400:
            raise RuntimeError(f"MCP HTTP 服务器返回 {response.status_code}: {response.text[:300]}")

        return _parse_response_body(response)

    # ------------------------------------------------------------------
    # 其他
    # ------------------------------------------------------------------

    def get_type(self) -> str:
        return "http"


def _parse_response_body(response) -> dict:
    """解析 MCP HTTP 响应体（支持纯 JSON 与 SSE 两种格式）。

    以 Content-Type 判断为主，同时嗅探 body：Tavily 等服务偶发返回错误的
    Content-Type 但仍输出 SSE 文本（event: / data: 开头），双保险。
    """
    text = response.text
    content_type = response.headers.get("Content-Type", "")
    is_sse = "text/event-stream" in content_type
    if not is_sse:
        stripped = text.lstrip()
        is_sse = stripped.startswith("event:") or stripped.startswith("data:")

    if is_sse:
        # SSE：取最后一个 data: 行（请求-响应模型下通常只有一个）
        data_payload = ""
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("data:"):
                data_payload = line[len("data:"):].strip()
        if not data_payload:
            raise RuntimeError("MCP HTTP 服务器返回空的 SSE 响应")
        try:
            return json.loads(data_payload)
        except json.JSONDecodeError:
            raise RuntimeError(f"SSE data 解析失败: {data_payload[:200]}")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        raise RuntimeError(f"MCP HTTP 响应不是有效 JSON: {text[:200]}")
