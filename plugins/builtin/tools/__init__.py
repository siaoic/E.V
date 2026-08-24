"""本地 Function Call 工具包 —— 工具插件化（L3-C，对标 dsh Tools as Plugins）。

架构：
  - 每个工具 = plugins/builtin/tools/<name>/ 目录，index.py 提供 register(ctx) 入口：
        def register(ctx):
            ctx.tools.register(
                name="my_tool",
                description="...",
                parameters={"city": {"type": "string", "required": True}},
                execute=my_impl,          # 或 lambda args: my_impl(**args)
                timeout=10.0,             # 可选：参考超时（秒）
                enabled_by="TOOL_MY_ENABLED",   # 可选：控制开关的配置字段名
                requires="SOME_API_KEY",        # 可选：依赖的外部 key 配置字段名
            )
  - 本包只负责扫描目录 + 调 register(ctx)，构建 _TOOL_CATALOG；
    _LOCAL_REGISTRY / defs.py 硬编码映射已删除（新增工具丢目录即用）。
  - get_merged_tools(mcp)  合并「MCP 工具 + 插件工具 + 本地工具」→ OpenAI tools 格式
  - call_tool(name, args, mcp)  MCP 优先，插件兜底，本地兜底（对标 tool-executor.js）
  - MCP 相关编排统一走 src/mcp/llm_bridge.py（本模块只保留本地工具逻辑）

可用性门控（等价旧 TOOL_*_ENABLED 开关 + key 过滤）：
  - enabled_by：工具依赖的配置开关字段名（如 TOOL_GET_WEATHER_ENABLED）；
  - requires：工具依赖的外部 key 配置字段名（如 OPENWEATHERMAP_API_KEY），
    未配置则不可用（等价旧「无 key 的工具跳过」）。
"""

from __future__ import annotations

import importlib.util
import inspect
import os
from typing import Callable, Dict, List

from ev.agent.tool_registry import _expand_parameters
from ev.utils import config, console

from ev.mcp.llm_bridge import call_mcp_tool, get_mcp_tools_for_llm
from plugins.manager import get_default_manager, tool_name

__all__ = [
    "get_merged_tools",
    "call_tool",
    "get_local_tool_names",
    "render_tool_guide",
    "get_tool_catalog",
    "_TOOL_CATALOG",
]


# ---------------------------------------------------------------------------
# 工具目录（name → 注册条目）；由各工具目录的 register(ctx) 填充（幂等加载）
# ---------------------------------------------------------------------------

_TOOL_CATALOG: Dict[str, dict] = {}
_LOADED = False


class _ToolRegistry:
    """本地 ctx.tools 目录：register() 收集条目到 _TOOL_CATALOG。

    enabled_by / requires 为配置字段名（config.cfg 属性），get_merged_tools
    按它们门控（等价旧 TOOL_*_ENABLED 开关 + key 过滤逻辑）。
    """

    def register(
        self,
        name: str,
        description: str,
        parameters: dict,
        execute: Callable,
        timeout: float = 10.0,
        enabled_by: str = "",
        requires: str = "",
    ) -> None:
        _TOOL_CATALOG[name] = {
            "def": {
                "type": "function",
                "function": {
                    "name": name,
                    "description": description,
                    "parameters": _expand_parameters(parameters),
                },
            },
            "execute": execute,      # execute(args: dict) -> Any（允许同步/异步）
            "timeout": timeout,
            "enabled_by": enabled_by or "",
            "requires": requires or "",
        }


class _ToolContext:
    """工具注册上下文：ctx.tools 提供与插件 ctx.tools.register 相同的接口。"""

    def __init__(self) -> None:
        self.tools = _ToolRegistry()


def _load_tool_module(index_path: str):
    """用 importlib 加载工具目录的 index.py（每次全新执行，热重载不命中缓存）。"""
    module_name = "tool_" + os.path.basename(
        os.path.dirname(index_path)) + "_" + hex(abs(hash(index_path)))[2:]
    spec = importlib.util.spec_from_file_location(module_name, index_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载工具模块：{index_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_tool_catalog() -> None:
    """扫描 plugins/builtin/tools/*/index.py，调 register(ctx) 构建目录（幂等）。"""
    global _LOADED
    if _LOADED:
        return
    _LOADED = True
    base = os.path.dirname(os.path.abspath(__file__))
    ctx = _ToolContext()
    for entry in sorted(os.listdir(base)):
        index_path = os.path.join(base, entry, "index.py")
        if not os.path.isfile(index_path):
            continue
        try:
            module = _load_tool_module(index_path)
        except Exception as e:
            console.warn(f"[工具] 加载 {entry}/index.py 失败：{e}")
            continue
        register = getattr(module, "register", None)
        if not callable(register):
            console.warn(f"[工具] 跳过 {entry}/：index.py 缺少 register(ctx) 入口")
            continue
        try:
            register(ctx)
        except Exception as e:
            console.warn(f"[工具] 注册 {entry}/ 失败：{e}")


def get_tool_catalog() -> Dict[str, dict]:
    """确保工具目录已加载并返回（name → 注册条目）。"""
    _load_tool_catalog()
    return _TOOL_CATALOG


def _is_available(name: str) -> bool:
    """按目录条目的 enabled_by / requires 元数据判断工具当前是否可用。

    enabled_by：控制开关的配置字段名（如 TOOL_GET_WEATHER_ENABLED）；
    requires：依赖的外部 key 配置字段名（如 OPENWEATHERMAP_API_KEY），
    未配置则不可用（等价旧「无 key 的工具跳过」）。
    """
    entry = _TOOL_CATALOG.get(name)
    if entry is None:
        return False
    if entry.get("enabled_by") and not getattr(config.cfg, entry["enabled_by"], False):
        return False
    if entry.get("requires") and not getattr(config.cfg, entry["requires"], ""):
        return False
    return True


# ---------------------------------------------------------------------------
# ToolRegistry 注册（3.2）：本地内置工具注册进统一注册表（门控 + JSON 归一化）
# ---------------------------------------------------------------------------

def _register_local_tools() -> None:
    """把本地内置工具注册进 ToolRegistry（幂等：已注册跳过）。

    handler 用目录条目的 execute（execute(args: dict)，允许同步/异步），
    不改动各工具实现本身（红线：行为 100% 不变）；toolset 统一 "local"。
    """
    from ev.agent.tool_registry import tool_registry
    for name, entry in _TOOL_CATALOG.items():
        if tool_registry.get_entry(name) is not None:
            continue
        function = entry["def"].get("function") or {}
        tool_registry.register(
            name,
            "local",
            function,
            handler=entry["execute"],
        )


# ---------------------------------------------------------------------------
# 对外接口
# ---------------------------------------------------------------------------


def get_merged_tools(mcp=None, toolset: str = "") -> List[dict]:
    """合并本地工具 + MCP 工具 → OpenAI Function Calling 格式。

    对标 live-2d(2) 的 getMergedToolsList()：MCP 工具优先，本地工具兜底。
    toolset 非空时按工具集门控过滤（3.3，仅影响内置工具；MCP/插件不受影响）；
    为空 = 全量，等价旧行为。
    """
    tools: List[dict] = []

    # 工具总开关（设置页「启动工具」）：关闭 → 本地与 MCP 工具全部停用
    if not config.cfg.TOOLS_ENABLED:
        return tools

    # MCP 工具（外部服务器提供）；MCP 相关编排集中在 src/mcp/llm_bridge.py
    tools.extend(get_mcp_tools_for_llm(mcp))

    # 插件工具（MCP 优先、插件次之、本地兜底；与 MCP/本地重名跳过）
    existing_names = {t["function"]["name"] for t in tools}
    pm = get_default_manager()
    if pm is not None:
        for tool_def in pm.get_all_tools():
            name = tool_name(tool_def)
            if name and name not in existing_names:
                tools.append(tool_def)
                existing_names.add(name)

    # 本地工具：目录即注册，可用性按 enabled_by/requires 门控
    # （等价旧 TOOL_*_ENABLED 开关 + key 过滤）；与 MCP 重名跳过
    _load_tool_catalog()
    existing_names = {t["function"]["name"] for t in tools}
    for name in sorted(_TOOL_CATALOG):
        if name in existing_names or not _is_available(name):
            continue
        tools.append(_TOOL_CATALOG[name]["def"])

    # 工具集门控（3.3）：非空 toolset 只暴露该场景内置工具；空 = 全量（旧行为）
    if toolset:
        from ev.agent.toolsets import filter_tool_defs
        tools = filter_tool_defs(tools, toolset)

    return tools


async def call_tool(name: str, args: dict, mcp=None) -> str:
    """执行工具调用：MCP 优先，插件兜底，本地兜底（对标 live-2d(2) tool-executor.js）。"""
    # 工具总开关关闭 → 拒绝调用
    if not config.cfg.TOOLS_ENABLED:
        return "错误：工具系统已关闭（设置页「启动工具」未开启），无法调用工具。"
    # 1) 优先 MCP（桥接在 src/mcp/llm_bridge.py，未命中返回 None 走本地兜底）
    mcp_result = await call_mcp_tool(name, args, mcp)
    if mcp_result is not None:
        return mcp_result

    # 2) 插件工具兜底（MCP 之后、本地之前；无插件提供时返回 None）
    pm = get_default_manager()
    if pm is not None:
        plugin_result = await pm.execute_tool(name, args or {})
        if plugin_result is not None:
            return plugin_result

    # 3) 本地工具：TOOL_REGISTRY 开启时走注册表（统一门控 + JSON 归一化）；
    #    未注册（未开启）回退直连目录实现（行为 100% 不变）
    _load_tool_catalog()
    if config.cfg.TOOL_REGISTRY:
        _register_local_tools()
        from ev.agent.tool_registry import tool_registry
        registry_result = await tool_registry.dispatch_async(name, args or {})
        if registry_result is not None:
            return registry_result

    entry = _TOOL_CATALOG.get(name)
    if entry is not None:
        result = entry["execute"](args or {})
        if inspect.isawaitable(result):
            result = await result
        return result

    return f"错误：找不到工具「{name}」，且 MCP 服务器未提供该工具。"


def get_local_tool_names() -> List[str]:
    """当前可用的本地工具名列表（受 enabled_by 开关与 requires key 过滤）。"""
    if not config.cfg.TOOLS_ENABLED:
        return []
    _load_tool_catalog()
    return [name for name in sorted(_TOOL_CATALOG) if _is_available(name)]


def render_tool_guide(tools: List[dict]) -> str:
    """从 OpenAI 工具定义生成「可用工具 + 使用时机」清单（注入系统提示用）。

    与 get_merged_tools 共用同一份 tools 列表（已过滤开关/key，只列模型真正
    可调用的工具）；description 已含触发时机语义（如「当用户问你看到什么时
    调用」），压缩成单行逐条列出——让模型明确知道何时该调用哪个工具，弥补
    「工具使用」引导段只有泛泛说明、不列具体清单的缺口（对标技能段的可用
    技能清单呈现）。
    """
    entries: List[str] = []
    for tool_def in tools:
        function = tool_def.get("function")
        if isinstance(function, dict):
            name = (function.get("name") or "").strip()
            description = (function.get("description") or "").strip()
        else:
            # 兼容 {name, description} 平铺格式（部分插件工具）
            name = (tool_def.get("name") or "").strip()
            description = (tool_def.get("description") or "").strip()
        if not name:
            continue
        description = " ".join(description.split())
        entries.append(f"- {name}: {description}")
    if not entries:
        return ""
    return ("可用工具（工具名 + 使用时机，情境匹配时优先调用）：\n"
            + "\n".join(entries))
