"""MCP Stdio 传输层 —— 严格参考 live-2d(2) 的 mcp-stdio-transport.js。

职责：spawn 子进程、JSON-RPC 2.0 逐行通信、工具调用。

关键设计（吸取本项目踩过的坑）：
  - 自实现 JSON-RPC，不依赖官方 mcp SDK —— 彻底规避「根目录 mcp/ 目录顶掉
    PyPI mcp 包」和「SDK 只继承白名单环境变量导致 TAVILY_API_KEY 丢失」两类问题。
  - 环境变量白名单：只透传本服务用得到的 key，其它 .env 密钥（LLM_API_KEY /
    BILI_SESSDATA 等）一律不传子进程，避免用户从网上下载的第三方 tools 脚本
    直接 exfil 密钥。子进程需要更多 key 时由用户在 mcp_config.json 里显式
    env 字段声明（config.env 始终生效）。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from typing import List, Optional

from src.utils import console
from src.mcp.registry import MCPToolRegistry


# 白名单：仅透传本服务声明需要的 key。
# 子进程需要更多 key 时由用户在 mcp_config.json 里显式 env 字段声明。
_SAFE_ENV_KEYS = (
    "PATH", "LANG", "LC_ALL", "TEMP", "TMP", "USERPROFILE",
    "TAVILY_API_KEY", "OPENWEATHERMAP_API_KEY",  # 远程 MCP 显式需要
    "SILICONFLOW_API_KEY",                      # 嵌入 / STT
    "LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL", # 本地 LLM 转发
)


class MCPStdioTransport:
    """MCP stdio 传输：管理子进程与 JSON-RPC 通信。"""

    def __init__(
        self,
        server_config: dict,
        tool_registry: MCPToolRegistry,
        timeout: float = 30.0,
    ) -> None:
        self.config = server_config
        self.tool_registry = tool_registry
        self.timeout = timeout
        self.proc: Optional[asyncio.subprocess.Process] = None
        self.server_name: str = ""

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def start(self, server_name: str) -> None:
        """启动子进程并完成 initialize + tools/list 握手。"""
        self.server_name = server_name
        command = self.config.get("command", "")
        args = list(self.config.get("args", []) or [])

        if not command:
            raise RuntimeError(f"MCP 服务器 {server_name} 缺少 command")

        # 工作目录：相对路径命令（./tools/...）默认以 MCP 配置目录为 cwd
        cwd = self.config.get("cwd") or os.getcwd()

        console.info(f"🚀 启动 MCP Stdio 服务器: {server_name} -> {command}")

        # 环境变量：白名单（仅透传 _SAFE_ENV_KEYS）+ 用户在 mcp_config.json
        # 里显式声明的 config.env（始终生效）。避免第三方 tools 脚本读到
        # .env 里的 LLM_API_KEY / BILI_SESSDATA 等敏感密钥。
        env = {k: v for k, v in os.environ.items() if k in _SAFE_ENV_KEYS}
        env.update(self.config.get("env") or {})

        # Windows：subprocess.CREATE_NO_WINDOW 防止 Python 子进程拉起时
        # 控制台窗口闪烁（与 L2 一致）
        popen_kwargs: dict = {}
        if sys.platform == "win32":
            popen_kwargs["creationflags"] = (
                getattr(asyncio.subprocess, "CREATE_NO_WINDOW", 0x08000000)
            )

        self.proc = await asyncio.create_subprocess_exec(
            command,
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=cwd,
            **popen_kwargs,
        )

        # 后台读取 stderr，避免管道填满阻塞子进程
        self._stderr_task = asyncio.create_task(self._drain_stderr())

        # 1) initialize 握手
        init_resp = await self._request(
            "initialize",
            params={
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "vtuber-mcp-client", "version": "1.0.0"},
            },
            timeout=self.timeout,
        )
        if init_resp.get("error"):
            raise RuntimeError(f"MCP 服务器 {server_name} initialize 失败: {init_resp['error']}")

        # 2) 拉取工具列表
        tools_resp = await self._request("tools/list", params={}, timeout=self.timeout)
        if tools_resp.get("error"):
            raise RuntimeError(f"MCP 服务器 {server_name} tools/list 失败: {tools_resp['error']}")

        server_tools: List[dict] = (tools_resp.get("result") or {}).get("tools", []) or []
        self.tool_registry.register_tools(server_name, server_tools, "mcp")
        console.ok(f"MCP Stdio 服务器 {server_name} 就绪，{len(server_tools)} 个工具")

    async def call_tool(self, tool_name: str, args: dict) -> str:
        """调用工具，返回可直接朗读的文本结果。"""
        if not self.proc or self.proc.returncode is not None:
            raise RuntimeError(f"MCP 服务器未启动: {self.server_name}")

        resp = await self._request(
            "tools/call",
            params={"name": tool_name, "arguments": args},
            timeout=self.timeout,
        )
        if resp.get("error"):
            raise RuntimeError(f"工具 {tool_name} 调用失败: {resp['error'].get('message', resp['error'])}")

        result = resp.get("result") or {}
        content = result.get("content") or []
        # 取 text 类型内容；无则返回 JSON 序列化
        # 外部子进程返回内容一律过 sanitize，防 prompt-injection 污染
        from src.utils.safe_text import sanitize_external
        text_parts = [
            sanitize_external(c.get("text", ""))
            for c in content
            if c.get("type") == "text" and c.get("text")
        ]
        if text_parts:
            return "\n".join(text_parts)
        is_error = result.get("isError")
        fallback = json.dumps(result, ensure_ascii=False)[:2000]
        return f"工具返回了 {'错误' if is_error else '结果'}: {sanitize_external(fallback)}"

    async def stop(self) -> None:
        """终止子进程。"""
        # 先取消后台 stderr 读取任务，避免 loop 关闭后管道回调报错
        task = getattr(self, "_stderr_task", None)
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        if self.proc and self.proc.returncode is None:
            try:
                self.proc.terminate()
                try:
                    await asyncio.wait_for(self.proc.wait(), timeout=3)
                except asyncio.TimeoutError:
                    self.proc.kill()
                console.dim(f"🛑 MCP Stdio 服务器 {self.server_name} 已停止")
            except Exception as e:
                console.warn(f"停止 MCP 服务器 {self.server_name} 失败: {e}")
        self.proc = None

    # ------------------------------------------------------------------
    # JSON-RPC 通信
    # ------------------------------------------------------------------

    async def _request(self, method: str, params: dict, timeout: float) -> dict:
        """发送 JSON-RPC 请求并等待匹配 id 的响应。"""
        req_id = f"{method}_{self.server_name}_{uuid.uuid4().hex[:8]}"
        payload = json.dumps(
            {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}
        )
        if self.proc is None or self.proc.stdin is None or self.proc.stdout is None:
            raise RuntimeError("MCP 子进程未就绪")

        self.proc.stdin.write((payload + "\n").encode("utf-8"))
        await self.proc.stdin.drain()

        while True:
            try:
                line = await asyncio.wait_for(self.proc.stdout.readline(), timeout=timeout)
            except asyncio.TimeoutError:
                raise RuntimeError(f"工具调用超时: {method}")

            if not line:
                raise RuntimeError(f"MCP 子进程 {self.server_name} 已退出")

            text = line.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            try:
                resp = json.loads(text)
            except json.JSONDecodeError:
                continue  # 跳过非 JSON 输出（如启动横幅）

            # 只响应匹配的 id；notification（无 id）直接跳过
            if resp.get("id") == req_id:
                return resp

    async def _drain_stderr(self) -> None:
        """后台读取 stderr，防止管道阻塞。"""
        try:
            while self.proc is not None and self.proc.stderr is not None:
                data = await self.proc.stderr.read(4096)
                if not data:
                    break
                console.dim(f"[MCP:{self.server_name}] {data.decode('utf-8', errors='replace').strip()}")
        except Exception:
            pass
