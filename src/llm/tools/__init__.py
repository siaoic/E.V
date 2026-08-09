"""本地 Function Call 工具包 —— 严格参考 live-2d(2) 的 web-search 插件与 tool-executor.js。

架构（与 live-2d(2) 对齐）：
  - 每个工具 = OpenAI function 定义（defs.py）+ 异步实现（httpx，不阻塞 asyncio 主循环）
  - get_merged_tools(mcp)  合并「本地工具 + MCP 工具」→ OpenAI tools 格式
  - call_tool(name, args)  MCP 优先，本地兜底（对标 tool-executor.js）

工具（src/llm/tools/ 下各一模块）：
  - web_search(query)    Tavily 直调：AI 摘要前置 + 详细结果（可直接朗读中文）
  - get_current_time()   当前时间
  - get_weather(city)    OpenWeatherMap Geocoding + One Call 3.0
  - load_skill(name)     按名加载技能完整指令（严格参照 Muika _skill.py）
  - read_skill_resource(name, path)  按相对路径读取技能捆绑资源（渐进式披露）
  - skills.py           技能管理器（技能注册表 + watchdog 热重载，技能工具的数据源）
"""

from __future__ import annotations

import json
from typing import List

from src.utils import config, console

from src.llm.tools.defs import _LOCAL_TOOL_DEFS
from src.llm.tools.web_search import _web_search
from src.llm.tools.time import _get_current_time
from src.llm.tools.weather import _get_weather
from src.llm.tools.skill_loader import _load_skill, _read_skill_resource

__all__ = [
    "get_merged_tools",
    "call_tool",
    "get_local_tool_names",
    "_LOCAL_TOOL_DEFS",
    "_LOCAL_REGISTRY",
]


# ---------------------------------------------------------------------------
# 注册表：name → 实现
# ---------------------------------------------------------------------------

_LOCAL_REGISTRY = {
    "web_search": _web_search,
    "get_current_time": _get_current_time,
    "get_weather": _get_weather,
    "load_skill": _load_skill,
    "read_skill_resource": _read_skill_resource,
}


# ---------------------------------------------------------------------------
# 对外接口
# ---------------------------------------------------------------------------


def get_merged_tools(mcp=None) -> List[dict]:
    """合并本地工具 + MCP 工具 → OpenAI Function Calling 格式。

    对标 live-2d(2) 的 getMergedToolsList()：MCP 工具优先，本地工具兜底。
    """
    tools: List[dict] = []

    # 工具总开关（设置页「启动工具」）：关闭 → 本地与 MCP 工具全部停用
    if not config.cfg.TOOLS_ENABLED:
        return tools

    # MCP 工具（外部服务器提供）
    if mcp is not None:
        mcp_tools = mcp.get_tools_for_llm()
        if mcp_tools:
            tools.extend(mcp_tools)

    # 本地工具：受 .env 的 TOOL_*_ENABLED 开关控制（控制中心「工具屋」勾选），
    # 无 key 的工具跳过（web_search / get_weather 依赖 key）
    available_names = set()
    if config.cfg.TOOL_WEB_SEARCH_ENABLED and config.cfg.TAVILY_API_KEY:
        available_names.add("web_search")
    if config.cfg.TOOL_GET_CURRENT_TIME_ENABLED:
        available_names.add("get_current_time")
    if config.cfg.TOOL_GET_WEATHER_ENABLED and config.cfg.OPENWEATHERMAP_API_KEY:
        available_names.add("get_weather")
    # load_skill / read_skill_resource 常驻（技能系统不依赖外部 key，对标 Muika），
    # 但可被工具屋关闭
    if config.cfg.TOOL_LOAD_SKILL_ENABLED:
        available_names.add("load_skill")
        available_names.add("read_skill_resource")

    # MCP 已提供 Tavily 官方搜索（tavily-search/tavily-extract）时，
    # 隐藏本地 web_search，避免两个搜索工具让 LLM 选择混乱（对标 Tavily MCP 文档）
    mcp_names = {t["function"]["name"] for t in tools}
    if any("tavily" in n or "search" in n for n in mcp_names):
        available_names.discard("web_search")

    # 与 MCP 工具重名的本地工具跳过（外部服务器优先，避免 LLM 调用歧义）
    existing_names = {t["function"]["name"] for t in tools}
    for tool_def in _LOCAL_TOOL_DEFS:
        name = tool_def["function"]["name"]
        if name in available_names and name not in existing_names:
            tools.append(tool_def)

    return tools


async def call_tool(name: str, args: dict, mcp=None) -> str:
    """执行工具调用：MCP 优先，本地兜底（对标 live-2d(2) tool-executor.js）。"""
    # 工具总开关关闭 → 拒绝调用
    if not config.cfg.TOOLS_ENABLED:
        return "错误：工具系统已关闭（设置页「启动工具」未开启），无法调用工具。"
    # 1) 优先 MCP
    if mcp is not None and mcp.is_enabled:
        mcp_results = await mcp.handle_tool_calls(
            [{"id": f"local_{name}", "type": "function",
              "function": {"name": name, "arguments": json.dumps(args or {}, ensure_ascii=False)}}]
        )
        if mcp_results:
            return mcp_results[0]["content"]

    # 2) 本地工具兜底
    impl = _LOCAL_REGISTRY.get(name)
    if impl is not None:
        return await impl(**(args or {}))

    return f"错误：找不到工具「{name}」，且 MCP 服务器未提供该工具。"


def get_local_tool_names() -> List[str]:
    """当前可用的本地工具名列表（受 TOOL_*_ENABLED 开关与 key 过滤）。"""
    names = set()
    if not config.cfg.TOOLS_ENABLED:
        return []
    if config.cfg.TOOL_WEB_SEARCH_ENABLED and config.cfg.TAVILY_API_KEY:
        names.add("web_search")
    if config.cfg.TOOL_GET_CURRENT_TIME_ENABLED:
        names.add("get_current_time")
    if config.cfg.TOOL_GET_WEATHER_ENABLED and config.cfg.OPENWEATHERMAP_API_KEY:
        names.add("get_weather")
    if config.cfg.TOOL_LOAD_SKILL_ENABLED:
        names.add("load_skill")
        names.add("read_skill_resource")
    return sorted(names)
