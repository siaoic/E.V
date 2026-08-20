"""本地 Function Call 工具包 —— 严格参考 live-2d(2) 的 web-search 插件与 tool-executor.js。

架构（与 live-2d(2) 对齐）：
  - 每个工具 = OpenAI function 定义（defs.py）+ 异步实现（httpx，不阻塞 asyncio 主循环）
  - get_merged_tools(mcp)  合并「MCP 工具 + 插件工具 + 本地工具」→ OpenAI tools 格式
  - call_tool(name, args)  MCP 优先，插件兜底，本地兜底（对标 tool-executor.js）
  - MCP 相关编排统一走 src/mcp/llm_bridge.py（本模块只保留本地工具逻辑）

工具（plugins/tools/ 下各一模块）：
  - 联网搜索走 MCP 的 bing-cn-mcp（bing_search，见 src/mcp/mcp_config.json）
  - get_current_time()   当前时间
  - get_weather(city)    OpenWeatherMap Geocoding + One Call 3.0
  - load_skill(name)     按名加载技能完整指令（严格参照 Muika _skill.py）
  - read_skill_resource(name, path)  按相对路径读取技能捆绑资源（渐进式披露）
  - skills.py           技能管理器（技能注册表 + watchdog 热重载，技能工具的数据源）
  - memory_tools.py     记忆管理：remember_fact / forget_memory（LLM 自行判断写入/遗忘）
"""

from __future__ import annotations

from typing import List

from src.utils import config, console

from plugins.tools.defs import _LOCAL_TOOL_DEFS
from plugins.tools.time import _get_current_time
from plugins.tools.weather import _get_weather
from plugins.tools.skill_loader import _load_skill, _read_skill_resource
from plugins.tools.memory_tools import _remember_fact, _forget_memory
from plugins.tools.screen import _look_at_screen
from plugins.tools.sfx import _play_sound_effect, _list_sound_effects
from plugins.tools.diary import _write_diary
from src.mcp.llm_bridge import call_mcp_tool, get_mcp_tools_for_llm
from plugins.manager import get_default_manager, tool_name

__all__ = [
    "get_merged_tools",
    "call_tool",
    "get_local_tool_names",
    "render_tool_guide",
    "_LOCAL_TOOL_DEFS",
    "_LOCAL_REGISTRY",
]


# ---------------------------------------------------------------------------
# 注册表：name → 实现
# ---------------------------------------------------------------------------

_LOCAL_REGISTRY = {
    "get_current_time": _get_current_time,
    "get_weather": _get_weather,
    "load_skill": _load_skill,
    "read_skill_resource": _read_skill_resource,
    "remember_fact": _remember_fact,
    "forget_memory": _forget_memory,
    "look_at_screen": _look_at_screen,
    "play_sound_effect": _play_sound_effect,
    "list_sound_effects": _list_sound_effects,
    "write_diary": _write_diary,
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

    # 本地工具：受 .env 的 TOOL_*_ENABLED 开关控制（控制中心「插件」页勾选），
    # 无 key 的工具跳过（get_weather 依赖 key）
    available_names = set()
    if config.cfg.TOOL_GET_CURRENT_TIME_ENABLED:
        available_names.add("get_current_time")
    if config.cfg.TOOL_GET_WEATHER_ENABLED and config.cfg.OPENWEATHERMAP_API_KEY:
        available_names.add("get_weather")
    # load_skill / read_skill_resource 常驻（技能系统不依赖外部 key，对标 Muika），
    # 但可被工具屋关闭
    if config.cfg.TOOL_LOAD_SKILL_ENABLED:
        available_names.add("load_skill")
        available_names.add("read_skill_resource")
    # 记忆工具跟随记忆系统开关（LLM 自行判断何时「记住/忘掉」）
    if config.cfg.MEMORY_ENABLED:
        available_names.add("remember_fact")
        available_names.add("forget_memory")
    # 屏幕视觉（截屏 + 多模态描述），不依赖外部 key
    if config.cfg.TOOL_LOOK_SCREEN_ENABLED:
        available_names.add("look_at_screen")
    # 音效播放（本地 wav，无外部依赖；列表工具随播放开关）
    if config.cfg.TOOL_PLAY_SFX_ENABLED:
        available_names.add("play_sound_effect")
        available_names.add("list_sound_effects")
    # 写日记（LLM 自行判断何时记录当天，素材取 memory 会话轮次）
    if config.cfg.TOOL_WRITE_DIARY_ENABLED:
        available_names.add("write_diary")

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

    # 3) 本地工具兜底
    impl = _LOCAL_REGISTRY.get(name)
    if impl is not None:
        return await impl(**(args or {}))

    return f"错误：找不到工具「{name}」，且 MCP 服务器未提供该工具。"


def get_local_tool_names() -> List[str]:
    """当前可用的本地工具名列表（受 TOOL_*_ENABLED 开关与 key 过滤）。"""
    names = set()
    if not config.cfg.TOOLS_ENABLED:
        return []
    if config.cfg.TOOL_GET_CURRENT_TIME_ENABLED:
        names.add("get_current_time")
    if config.cfg.TOOL_GET_WEATHER_ENABLED and config.cfg.OPENWEATHERMAP_API_KEY:
        names.add("get_weather")
    if config.cfg.TOOL_LOAD_SKILL_ENABLED:
        names.add("load_skill")
        names.add("read_skill_resource")
    if config.cfg.MEMORY_ENABLED:
        names.add("remember_fact")
        names.add("forget_memory")
    if config.cfg.TOOL_LOOK_SCREEN_ENABLED:
        names.add("look_at_screen")
    if config.cfg.TOOL_PLAY_SFX_ENABLED:
        names.add("play_sound_effect")
        names.add("list_sound_effects")
    if config.cfg.TOOL_WRITE_DIARY_ENABLED:
        names.add("write_diary")
    return sorted(names)


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
