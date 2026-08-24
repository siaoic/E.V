"""MCP Stdio 传输层 —— 基于官方 mcp SDK（mcp.client.stdio）。

职责：把 mcp_config.json 里的 stdio 服务器配置交给官方 SDK 拉起子进程并通信。
相比早期自研 JSON-RPC 的优势：
  - 官方维护握手 / 协议版本协商 / 进程树清理（Windows Job Object）
  - Windows 上自动解析 npx/npm 等 .cmd 命令（shutil.which 兜底），无需 cmd /c 包装

环境变量：官方 SDK 默认只继承系统路径类白名单（DEFAULT_INHERITED_ENV_VARS），
这里补充本项目需要透传的 key（_SAFE_ENV_KEYS）+ 用户在 mcp_config.json 显式声明的
config.env。LLM_API_KEY 仅在子进程确实需要时才透传，BILI_SESSDATA 等敏感密钥不进
子进程，避免第三方 tools 脚本 exfil 密钥。
"""

from __future__ import annotations

import os
import shutil
import sys

from ev.utils import console
from ev.kernel.exceptions import EVBaseException, ErrorCode
from ev.mcp._base import MCPClientTransportBase
from ev.mcp.registry import MCPToolRegistry

# 官方 SDK 默认白名单之外、本项目需要透传给子进程的 key
_SAFE_ENV_KEYS = (
    "LANG", "LC_ALL", "TMP",
    "OPENWEATHERMAP_API_KEY",              # 远程 MCP 显式需要
    "SILICONFLOW_API_KEY",                 # 嵌入 / STT
    "LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL",  # 本地 LLM 转发
)


class MCPStdioTransport(MCPClientTransportBase):
    """MCP stdio 传输：官方 SDK 拉起子进程 + JSON-RPC 通信。"""

    tool_type = "mcp"

    def _create_client(self):
        command = self.config.get("command", "")
        args = list(self.config.get("args", []) or [])
        if not command:
            raise EVBaseException(
                ErrorCode.MCP_SERVER_FAILED,
                f"MCP 服务器 {self.server_name} 缺少 command")

        # 工作目录：相对路径命令（./tools/...）默认以 MCP 配置目录为 cwd
        cwd = self.config.get("cwd") or os.getcwd()

        console.info(f"🚀 启动 MCP Stdio 服务器: {self.server_name} -> {command}")

        # Windows 上 npx/npm 等 npm 脚本是 .cmd/.bat 或无扩展名脚本文件（如
        # nodejs 的 npx 无扩展名），CreateProcess 无法直接拉起（WinError 193）；
        # 解析结果非 .exe/.com 时统一包装为 cmd /c，由 cmd 按 PATHEXT 查找执行。
        if sys.platform == "win32":
            resolved = shutil.which(command)
            if resolved and os.path.splitext(resolved)[1].lower() not in (".exe", ".com"):
                args = ["/c", command] + args
                command = "cmd"

        # 环境变量：官方 SDK 白名单（系统路径类）+ 本项目需要透传的 key + 配置显式声明
        env = {k: v for k, v in os.environ.items() if k in _SAFE_ENV_KEYS}
        env.update(self.config.get("env") or {})

        from mcp import Client, StdioServerParameters
        from mcp.client.stdio import stdio_client

        params = StdioServerParameters(
            command=command, args=args, env=env or None, cwd=cwd,
        )
        return Client(stdio_client(params), read_timeout_seconds=self.timeout)

    def _ready_log(self, tool_count: int) -> None:
        console.ok(f"MCP Stdio 服务器 {self.server_name} 就绪，{tool_count} 个工具")

    def _stop_log(self) -> None:
        console.dim(f"🛑 MCP Stdio 服务器 {self.server_name} 已停止")
