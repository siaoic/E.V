"""MCP 客户端传输公共基类（官方 mcp SDK）。

统一生命周期管理：enter / exit 都在同一个后台 task 中执行，满足官方 SDK
（anyio cancel scope）「进入与退出必须在同一 task」的要求，避免
"Attempted to exit cancel scope in a different task" 警告。
start() 只等待就绪信号（含启动超时），stop() 通知退出并等待 task 收尾。
"""

from __future__ import annotations

import asyncio
import json
from typing import Optional

from src.utils import console
from src.core.exceptions import EVBaseException, ErrorCode
from src.mcp.registry import MCPToolRegistry


def _tool_to_dict(tool) -> dict:
    """官方 Tool 对象 → 注册表 dict 格式 {name, description, parameters}。"""
    schema = getattr(tool, "input_schema", None)
    dump = getattr(schema, "model_dump", None)
    parameters = dump(mode="json") if dump else {"type": "object"}
    return {
        "name": tool.name,
        "description": getattr(tool, "description", "") or "",
        "parameters": parameters,
    }


def _result_to_text(result) -> str:
    """CallToolResult → 可直接朗读的文本（外部内容过 sanitize 防注入）。"""
    from src.utils.safe_text import sanitize_external

    text_parts = []
    for content in getattr(result, "content", None) or []:
        text = getattr(content, "text", "")
        if text:
            text_parts.append(sanitize_external(text))
    if text_parts:
        return "\n".join(text_parts)

    structured = getattr(result, "structured_content", None)
    fallback = json.dumps(
        structured if structured is not None else {},
        ensure_ascii=False,
    )[:2000]
    is_error = getattr(result, "is_error", False)
    return f"工具返回了 {'错误' if is_error else '结果'}: {sanitize_external(fallback)}"


class MCPClientTransportBase:
    """官方 SDK 客户端传输公共基类（stdio / HTTP 共用生命周期）。"""

    # 子类指定：注册表里的工具类型标识
    tool_type = "mcp"

    def __init__(
        self,
        server_config: dict,
        tool_registry: MCPToolRegistry,
        timeout: float = 30.0,
    ) -> None:
        self.config = server_config
        self.tool_registry = tool_registry
        self.timeout = timeout
        self.server_name: str = ""
        # mcp.Client 实例；懒加载（未安装 mcp 时模块加载不报错，启动时才失败）
        self._client = None
        self._ready: Optional[asyncio.Event] = None
        self._stop_event: Optional[asyncio.Event] = None
        self._lifecycle_task: Optional[asyncio.Task] = None
        self._start_error: Optional[BaseException] = None

    # ------------------------------------------------------------------
    # 子类实现
    # ------------------------------------------------------------------

    def _create_client(self):
        """构建 mcp.Client（含传输层）。"""
        raise NotImplementedError

    def _ready_log(self, tool_count: int) -> None:
        raise NotImplementedError

    def _stop_log(self) -> None:
        raise NotImplementedError

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def start(self, server_name: str) -> None:
        """启动连接并等待就绪（含握手 + 工具注册）。超时/失败抛 RuntimeError。"""
        self.server_name = server_name
        self._client = self._create_client()
        self._ready = asyncio.Event()
        self._stop_event = asyncio.Event()
        self._start_error = None
        self._lifecycle_task = asyncio.create_task(self._lifecycle())

        try:
            await asyncio.wait_for(self._ready.wait(), timeout=self.timeout)
        except asyncio.TimeoutError:
            await self._terminate()
            raise EVBaseException(
                ErrorCode.MCP_SERVER_FAILED,
                f"服务器 {server_name} 启动超时（{self.timeout}s）")
        if self._start_error is not None:
            await self._terminate()
            raise EVBaseException(
                ErrorCode.MCP_SERVER_FAILED,
                f"服务器 {server_name} 启动失败: {self._start_error}")

    async def _lifecycle(self) -> None:
        """常驻生命周期：enter → 拉取工具 → 等待停止 → exit（同一 task）。"""
        try:
            await self._client.__aenter__()
        except BaseException as e:
            self._start_error = e
            self._ready.set()
            return
        try:
            tools = await self._client.list_tools()
            self.tool_registry.register_tools(
                self.server_name, [_tool_to_dict(t) for t in tools.tools], self.tool_type,
            )
            self._ready_log(len(tools.tools))
            self._ready.set()
            await self._stop_event.wait()
        except BaseException as e:
            self._start_error = e
            self._ready.set()
        finally:
            try:
                await self._client.__aexit__(None, None, None)
            except Exception:
                pass

    async def _terminate(self) -> None:
        """启动失败/超时兜底：取消生命周期并尽力清理进程。"""
        task, self._lifecycle_task = self._lifecycle_task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        client, self._client = self._client, None
        if client is not None:
            try:
                await client.__aexit__(None, None, None)
            except Exception:
                pass

    async def call_tool(self, tool_name: str, args: dict) -> str:
        """调用工具，返回可直接朗读的文本结果。"""
        if self._client is None:
            raise EVBaseException(
                ErrorCode.MCP_SERVER_FAILED,
                f"MCP 服务器未启动: {self.server_name}")
        result = await self._client.call_tool(tool_name, args or {})
        return _result_to_text(result)

    async def stop(self) -> None:
        """通知生命周期退出并等待收尾。"""
        task = self._lifecycle_task
        if task is not None:
            self._stop_event.set()
            try:
                await task
            except Exception as e:
                console.warn(f"停止 MCP 服务器 {self.server_name} 失败: {e}")
        self._stop_log()
        self._client = None
