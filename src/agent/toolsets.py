"""工具集门控（对标 Hermes toolsets.py 精简落地）。

3.3：工具只有名字出现在当前 toolset 才暴露给 agent，控制每轮 prompt 长度
与工具选择质量（全量 schema 会稀释工具选择，Prompt Cache 与准确率双受损）。

E.V 内置工具全量清单 + 按场景组合：
- live（默认）：全量内置工具，等价旧行为；
- pet：桌面陪伴场景（时间/天气/技能/音效/日记/屏幕，不含记忆类）；
- minimal：纯聊天必需（仅时间等极少量工具）。

回退：toolset 为空/未知 → 返回 None（全量），等价旧行为；MCP 工具与
插件工具不在内置清单中，toolset 过滤不涉及（始终保留）。
"""

from __future__ import annotations

from typing import Optional, Set

# E.V 内置工具全量清单（与 plugins/tools/defs.py 的 _LOCAL_TOOL_DEFS 对齐）
_EV_CORE_TOOLS: Set[str] = {
    "get_current_time",
    "get_weather",
    "load_skill",
    "read_skill_resource",
    "look_at_screen",
    "remember_fact",
    "forget_memory",
    "memory",
    "play_sound_effect",
    "list_sound_effects",
    "write_diary",
    "session_search",
}

# 场景组合（live = 全量；pet/minimal 为内置工具子集）
_TOOLSETS = {
    "live": set(_EV_CORE_TOOLS),
    "pet": {
        "get_current_time",
        "get_weather",
        "load_skill",
        "read_skill_resource",
        "play_sound_effect",
        "list_sound_effects",
        "write_diary",
    },
    "minimal": {"get_current_time"},
}


def get_toolset_names(toolset: Optional[str]) -> Optional[Set[str]]:
    """返回该 toolset 含的工具名集合；空/未知 toolset 返回 None（全量）。

    None 语义：调用方不做任何过滤，等价旧行为（回退路径）。
    """
    if not toolset:
        return None
    return _TOOLSETS.get(toolset.lower())


def filter_tool_defs(tool_defs: list, toolset: Optional[str]) -> list:
    """按 toolset 过滤 OpenAI 工具定义列表。

    仅过滤内置清单（_EV_CORE_TOOLS）内的工具；MCP/插件工具不受影响。
    toolset 为空/未知时原样返回（等价旧行为）。
    """
    names = get_toolset_names(toolset)
    if names is None:
        return tool_defs
    result = []
    for tool_def in tool_defs:
        function = tool_def.get("function") or {}
        name = function.get("name")
        if name in _EV_CORE_TOOLS and name not in names:
            continue  # 内置工具不在当前 toolset → 摘除
        result.append(tool_def)
    return result
