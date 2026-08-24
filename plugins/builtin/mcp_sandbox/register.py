"""MCP Client + Safe Sandbox 二合一骨架。"""
from __future__ import annotations

from typing import Any, Dict, List, Optional


def register(ctx) -> None:
    cfg = ctx.config
    impl_name_mcp: str = cfg.get("impl_name_mcp", "official-client")
    impl_name_sbx: str = cfg.get("impl_name_sandbox", "safe-sandbox")
    servers: Any = cfg.get("servers", {}) or {}
    allow_network: bool = bool(cfg.get("allow_network", False))
    memory_limit_mb: int = int(cfg.get("memory_limit_mb", 256))

    mcp = MCPClientStub(name=impl_name_mcp, servers=servers)
    sbx = SafeSandboxStub(
        name=impl_name_sbx,
        allow_network=allow_network,
        memory_limit_mb=memory_limit_mb,
    )

    try:
        from ev.kernel.slots import SlotName
    except Exception as e:
        ctx.log("error", f"无法导入 SlotName: {e}")
        return
    if ctx.slots is None:
        ctx.log("warn", "ctx.slots 为空，跳过 mcp/sandbox 注册")
        return
    try:
        ctx.slots.register(SlotName.mcp, impl_name_mcp, mcp)
        ctx.slots.register(SlotName.sandbox, impl_name_sbx, sbx)
    except Exception as e:
        ctx.log("error", f"注册 mcp/sandbox 失败: {e}")
        return
    ctx.log(
        "ok",
        f"已注册 MCP={impl_name_mcp}(servers={list(_keys(servers))}) Sandbox={impl_name_sbx}",
    )


def _keys(v: Any) -> List[str]:
    if isinstance(v, dict):
        return list(v.keys())
    if isinstance(v, list):
        return [str(x.get("name") if isinstance(x, dict) else x) for x in v]
    return []


class MCPClientStub:
    """MCP Contract 占位（name 字段即可满足）。"""

    def __init__(
        self,
        name: str = "official-client",
        servers: Any = None,
    ) -> None:
        self.name = name
        self.servers = servers if servers is not None else {}
        self._connected: bool = False
        self._tools: List[Dict[str, Any]] = []

    async def connect(self) -> None:
        self._connected = True

    async def close(self) -> None:
        self._connected = False

    async def list_tools(self) -> List[Dict[str, Any]]:
        return list(self._tools)

    async def call_tool(self, tool_name: str, arguments: Optional[Dict[str, Any]] = None) -> Any:
        return {"ok": True, "name": tool_name, "args": arguments or {}}


class SafeSandboxStub:
    """Sandbox Contract 占位（name 字段即可满足）。"""

    def __init__(
        self,
        name: str = "safe-sandbox",
        allow_network: bool = False,
        memory_limit_mb: int = 256,
    ) -> None:
        self.name = name
        self.allow_network = allow_network
        self.memory_limit_mb = memory_limit_mb
        self._running: bool = False

    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._running = False

    async def exec_code(self, code: str, timeout: float = 30.0) -> Dict[str, Any]:
        return {"ok": True, "stdout": "", "stderr": "", "exit_code": 0}
