"""MCP 管理器 —— 严格参考 live-2d(2) 的 mcp-manager.js。

职责：
  - 从外部 mcp_config.json 加载服务器配置（`_disabled` 后缀的服务器自动跳过）
  - 自动同步 tools 文件夹（*.py / *.js）到配置，新增/删除自动感知
  - 启动 stdio / HTTP 服务器并注册工具
  - 提供统一的工具调用接口（供 LLM function calling 执行）
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path
from typing import Dict, List, Optional

from ev.utils import config as vtuber_config
from ev.utils import console
from ev.kernel.exceptions import EVBaseException, ErrorCode
from ev.mcp.http_transport import MCPHttpTransport
from ev.mcp.registry import MCPToolRegistry
from ev.mcp.stdio_transport import MCPStdioTransport

# MCP 配置目录（重构后：configs/ 与 MCP_CONFIG_PATH 一致）
_MCP_DIR = os.path.dirname(os.path.abspath(vtuber_config.cfg.MCP_CONFIG_PATH))


_ENV_PLACEHOLDER_RE = re.compile(r"\$\{(\w+)\}")


def _unwrap_mcp_servers(config: dict) -> dict:
    """兼容标准 mcpServers 包装与平铺两种格式，返回服务器名→配置映射。"""
    servers = config.get("mcpServers")
    return servers if isinstance(servers, dict) else config


def _wrap_mcp_servers(config: dict, original: dict) -> dict:
    """写回时保持原文件的包装结构（mcpServers 或平铺）。"""
    if isinstance(original.get("mcpServers"), dict):
        return {"mcpServers": config}
    return config


def _expand_env(value):
    """递归展开字符串中的 ${ENV_VAR} 占位符（对标 live-2d(2) 的 env 注入方式）。

    用于 mcp_config.json 中需要从环境变量动态注入的系统字段 / 密钥，
    例如 stdio 服务器的 "SystemRoot": "${SystemRoot}"。
    注意用 `os.getenv(k) or ""`：env 里字段被清空时不会拿到默认值。
    """
    if isinstance(value, str):
        return _ENV_PLACEHOLDER_RE.sub(
            lambda m: os.getenv(m.group(1)) or "", value
        )
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    return value


class MCPManager:
    """MCP 管理器：加载配置 → 启动服务器 → 注册工具 → 调用工具。"""

    def __init__(self, config: Optional[dict] = None) -> None:
        cfg = config or {}
        self.cfg = cfg
        self.is_enabled: bool = cfg.get("enabled", vtuber_config.cfg.MCP_ENABLED)
        self.mcp_servers: Dict[str, dict] = {}
        self.transports: Dict[str, object] = {}
        self.tool_registry = MCPToolRegistry()
        self.is_initialized: bool = False
        self.startup_timeout: float = cfg.get("startup_timeout") or 30.0
        # 创建本实例的事件循环引用（initialize 时记录；Agent worker 线程跨循环用）
        self._loop = None

    # ------------------------------------------------------------------
    # 初始化
    # ------------------------------------------------------------------

    async def initialize(self) -> bool:
        """加载配置并启动所有服务器。失败不抛出，仅标记状态。"""
        # 记录创建本实例的事件循环：Agent worker 线程跨循环调 MCP 工具时
        # 桥回此循环执行（见 llm_bridge.call_mcp_tool 的线程安全桥）
        self._loop = asyncio.get_running_loop()
        if not self.is_enabled:
            self.is_initialized = True
            return True
        try:
            self._config_dir: Optional[str] = None
            self.load_mcp_config()
            await self.start_all_servers()
            self.is_initialized = True
            console.ok(
                f"MCP 管理器初始化完成: {self.tool_registry.get_tool_count()} 个工具可用"
            )
            return True
        except Exception as e:
            console.error(f"MCP 管理器初始化失败: {e}")
            self.is_initialized = True  # 即使失败也放行，避免阻塞主程序
            return False

    # ------------------------------------------------------------------
    # 配置加载
    # ------------------------------------------------------------------

    def load_mcp_config(self) -> None:
        """从外部配置文件或内嵌配置读取服务器列表。"""
        config_path = self.cfg.get("config_path") or vtuber_config.cfg.MCP_CONFIG_PATH

        if config_path and os.path.exists(config_path):
            self._config_dir = os.path.dirname(os.path.abspath(config_path))

            # 自动同步 tools 文件夹（对标 live-2d(2) autoSyncToolsFolder）
            self._auto_sync_tools_folder(config_path)

            with open(config_path, "r", encoding="utf-8") as f:
                loaded = json.load(f)

            # 兼容标准 mcpServers 包装与平铺两种格式
            servers = _unwrap_mcp_servers(loaded)

            # 过滤 _disabled 后缀服务器
            self.mcp_servers = {
                name: srv
                for name, srv in servers.items()
                if not name.endswith("_disabled")
            }
            console.info(
                f"📋 从 {config_path} 加载 MCP 配置，共 {len(self.mcp_servers)} 个服务器"
            )
            if self.mcp_servers:
                console.dim(f"MCP 服务器列表: {list(self.mcp_servers)}")
            return

        if self.cfg.get("servers"):
            self.mcp_servers = self.cfg["servers"]
            console.info(
                f"📋 从内嵌配置加载 MCP 配置，共 {len(self.mcp_servers)} 个服务器"
            )
            return

        console.warn("⚠️ 未找到 MCP 服务器配置，MCP 管理器将不提供工具")

    def _auto_sync_tools_folder(self, config_path: str) -> None:
        """扫描配置目录下的 tools 文件夹，自动同步为服务器配置。

        对标 live-2d(2) mcp-manager.js 的 autoSyncToolsFolder：
          - *.py / *.js 文件各自成为一个服务器（command + args）
          - 外部服务配置（非 tools/ 路径）原样保留
          - tools 中已删除的文件自动从配置移除
        """
        config_dir = os.path.dirname(config_path)
        tools_dir = os.path.join(config_dir, "tools")
        if not os.path.isdir(tools_dir):
            return

        try:
            with open(config_path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, json.JSONDecodeError):
            raw = {}

        # 解包 mcpServers 包装，统一按平铺格式处理；写回时恢复原包装结构
        config = _unwrap_mcp_servers(raw)
        wrapped = isinstance(raw.get("mcpServers"), dict)

        # 保留非 tools/ 的外部服务配置（如 bing-cn-mcp 等）
        external_configs = {}
        for key, srv in config.items():
            args = srv.get("args") or []
            if not (args and isinstance(args[0], str) and "tools/" in args[0]):
                external_configs[key] = srv

        # 扫描 tools 目录
        current_tools = []
        for item in sorted(os.listdir(tools_dir)):
            if item.endswith(".py"):
                current_tools.append({
                    "name": item[:-3],
                    "command": "python",
                    "args": [f"./tools/{item}"],
                })
            elif item.endswith(".js"):
                current_tools.append({
                    "name": item[:-3],
                    "command": "node",
                    "args": [f"./tools/{item}"],
                })

        # 清理已删除的工具
        for key in list(config):
            srv = config[key]
            args = srv.get("args") or []
            if args and isinstance(args[0], str) and args[0].startswith("./tools/"):
                if not any(t["name"] == key for t in current_tools):
                    console.dim(f"🗑️  删除不存在的工具: {key}")
                    del config[key]

        tool_configs = {}
        for t in current_tools:
            tool_configs[t["name"]] = {"command": t["command"], "args": t["args"]}
            if t["name"] not in config:
                console.dim(f"📦 自动添加工具: {t['name']}")

        final_config = {**external_configs, **tool_configs}
        if wrapped:
            final_config = {"mcpServers": final_config}
        try:
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump(final_config, f, ensure_ascii=False, indent=2)
        except OSError as e:
            console.warn(f"⚠️ 写入 MCP 配置失败: {e}")

        # 重新载入（含自动同步后的最新配置）
        with open(config_path, "r", encoding="utf-8") as f:
            loaded = _unwrap_mcp_servers(json.load(f))
        self.mcp_servers = {
            name: srv
            for name, srv in loaded.items()
            if not name.endswith("_disabled")
        }

    # ------------------------------------------------------------------
    # 服务器管理
    # ------------------------------------------------------------------

    async def start_all_servers(self) -> None:
        """并发启动所有服务器（任一台失败不阻塞其他）。"""
        tasks = [
            self.start_server(name, srv)
            for name, srv in self.mcp_servers.items()
        ]
        if not tasks:
            return
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for name, res in zip(self.mcp_servers, results):
            if isinstance(res, Exception):
                console.warn(f"⚠️ MCP 服务器 {name} 启动失败: {res}")

    async def start_server(self, name: str, server_config: dict) -> None:
        """启动单个服务器（stdio 或 HTTP）。

        超时取服务器配置的 timeout 字段（如 npx 首次下载 npm 包较慢），
        未配置时回退到全局 startup_timeout。
        """
        try:
            cfg = _expand_env(dict(server_config))  # 展开 ${ENV_VAR}（如 SystemRoot）
            timeout = float(cfg.get("timeout") or self.startup_timeout)
            # 相对路径命令（./tools/...）默认以 MCP 配置目录为 cwd
            args = cfg.get("args") or []
            if (args and isinstance(args[0], str) and args[0].startswith("./")
                    and self._config_dir):
                cfg.setdefault("cwd", self._config_dir)

            if cfg.get("type") == "streamable_http" or cfg.get("url"):
                transport = MCPHttpTransport(cfg, self.tool_registry, timeout)
            else:
                transport = MCPStdioTransport(cfg, self.tool_registry, timeout)
            # 启动超时由 transport.start 内部处理（ready 等待），这里不再包 wait_for，
            # 避免 Python 3.10 的 wait_for 把协程迁到新 task 导致 SDK cancel scope 不匹配
            await transport.start(name)
            self.transports[name] = transport
            self._open_autostart_url(name, cfg)
        except asyncio.TimeoutError:
            raise EVBaseException(
                ErrorCode.MCP_SERVER_FAILED, f"服务器 {name} 启动超时（{timeout}s）")

    def _open_autostart_url(self, name: str, cfg: dict) -> None:
        """服务器启动成功后按 autostart_url 配置自动打开本地页面。

        用于需要浏览器端配合的 MCP（如钢琴：打开 Piano.html 建立
        WebSocket 桥接后才能发声）。失败仅提示，不影响主程序。
        """
        url = cfg.get("autostart_url")
        if not url:
            return
        try:
            import webbrowser
            if "://" not in url:
                path = url if os.path.isabs(url) else os.path.join(
                    getattr(self, "_config_dir", None) or ".", url)
                url = Path(path).as_uri()
            webbrowser.open(url)
            console.dim(f"🌐 已为 MCP 服务器 {name} 打开页面: {url}")
        except Exception as e:
            console.warn(f"⚠️ 自动打开 {name} 页面失败（不影响运行）: {e}")

    # ------------------------------------------------------------------
    # 工具调用
    # ------------------------------------------------------------------

    async def call_mcp_tool(self, tool_name: str, args: dict) -> str:
        """调用 MCP 工具（内部方法）。"""
        tool = self.tool_registry.find_tool(tool_name)
        if not tool:
            raise EVBaseException(
                ErrorCode.TOOL_NOT_FOUND, f"MCP 工具未找到: {tool_name}")

        transport = self.transports.get(tool["server"])
        if not transport:
            raise EVBaseException(
                ErrorCode.MCP_SERVER_FAILED,
                f"MCP 服务器未找到: {tool['server']}")

        return await transport.call_tool(tool_name, args)

    def get_tools_for_llm(self) -> List[dict]:
        """返回 OpenAI Function Calling 格式的工具列表。"""
        if not self.is_enabled or self.tool_registry.get_tool_count() == 0:
            return []
        return self.tool_registry.to_openai_format()

    def warmup(self) -> None:
        """启动期预拉 MCP 工具列表（幂等），确认工具集可提供给 LLM。

        在 initialize（服务器全部启动、工具注册完毕）之后调用：把工具
        列表构建留在启动期而非首轮对话，同时提前暴露异常（仅 warn 不阻塞）。
        """
        if not self.is_enabled or not self.is_initialized:
            return
        try:
            tools = self.get_tools_for_llm()
            console.ok(f"MCP warmup 完成：{len(tools)} 个工具可提供给 LLM")
        except Exception as e:
            console.warn(f"[MCP] warmup 失败（不影响启动）：{e}")

    async def handle_tool_calls(self, tool_calls: list) -> Optional[list]:
        """处理 LLM 返回的工具调用（仅 MCP 工具）。

        返回 [{tool_call_id, content}, ...]；没有匹配的 MCP 工具时返回 None。
        """
        if not self.is_enabled or not tool_calls:
            return None

        results = []
        for tc in tool_calls:
            name = tc["function"]["name"]
            if not self.tool_registry.is_mcp_tool(name):
                continue
            try:
                args = json.loads(tc["function"]["arguments"] or "{}")
            except (json.JSONDecodeError, TypeError):
                args = {}
            try:
                result = await self.call_mcp_tool(name, args)
                results.append({"tool_call_id": tc.get("id"), "content": result})
            except Exception as e:
                console.error(f"MCP 工具 {name} 执行失败: {e}")
                results.append({"tool_call_id": tc.get("id"), "content": f"工具执行失败: {e}"})

        return results or None

    async def execute_function(self, tool_name: str, parameters: dict) -> str:
        """统一执行入口（供外部直接调用）。"""
        if not self.is_enabled:
            raise EVBaseException(ErrorCode.MCP_SERVER_FAILED, "MCP 管理器已禁用")
        return await self.call_mcp_tool(tool_name, parameters)

    # ------------------------------------------------------------------
    # 统计 / 清理
    # ------------------------------------------------------------------

    def get_stats(self) -> dict:
        stats = self.tool_registry.get_stats()
        return {
            "enabled": self.is_enabled,
            "initialized": self.is_initialized,
            "servers": len(self.mcp_servers),
            "tools": stats["total"],
            "tool_names": stats["tool_names"],
        }

    async def stop(self) -> None:
        """停止所有服务器。每步带超时兜底，防止卡死。"""
        stop_tasks = []
        for transport in self.transports.values():
            stop_tasks.append(asyncio.wait_for(transport.stop(), timeout=5))
        if stop_tasks:
            results = await asyncio.gather(*stop_tasks, return_exceptions=True)
            for res in results:
                if isinstance(res, Exception):
                    console.warn(f"MCP 服务器停止异常（忽略）: {res}")
        self.transports.clear()
        self.tool_registry.clear()
        console.dim("🔧 MCP 管理器已停止")


def get_mcp_dir() -> str:
    """返回 MCP 配置目录（vtuber/mcp/）。"""
    return _MCP_DIR
