"""toolsets 工具集门控单元测试（对标 Hermes toolsets.py）。

覆盖 3.3 验证点：
- live 全量 / pet / minimal 子集关系；
- filter_tool_defs 只过滤内置清单内工具（MCP/插件工具不受影响）；
- 空/未知 toolset 原样返回（回退 = 旧行为）。
"""
import pytest

from src.agent.toolsets import (
    _EV_CORE_TOOLS,
    filter_tool_defs,
    get_toolset_names,
)


def make_def(name):
    """构造一个 OpenAI 格式工具定义。"""
    return {"type": "function", "function": {"name": name, "description": "t"}}


class TestToolsetNames:
    def test_live_is_full(self):
        assert get_toolset_names("live") == set(_EV_CORE_TOOLS)

    def test_empty_returns_none(self):
        assert get_toolset_names("") is None
        assert get_toolset_names(None) is None

    def test_unknown_toolset_returns_none(self):
        assert get_toolset_names("bogus") is None

    def test_case_insensitive(self):
        assert get_toolset_names("Minimal") == {"get_current_time"}

    def test_pet_subset_of_live(self):
        pet = get_toolset_names("pet")
        assert pet.issubset(_EV_CORE_TOOLS)


class TestFilterToolDefs:
    def test_empty_toolset_no_filter(self):
        defs = [make_def("get_current_time"), make_def("write_diary")]
        assert filter_tool_defs(defs, "") == defs
        assert filter_tool_defs(defs, "bogus") == defs

    def test_minimal_keeps_only_core(self):
        defs = [
            make_def("get_current_time"),
            make_def("write_diary"),
            make_def("memory"),
        ]
        result = filter_tool_defs(defs, "minimal")
        names = [d["function"]["name"] for d in result]
        assert names == ["get_current_time"]

    def test_non_builtin_tools_kept(self):
        # MCP/插件工具不在内置清单 → toolset 过滤不涉及
        defs = [
            make_def("bing_search"),  # MCP 工具
            make_def("write_diary"),  # 内置工具
        ]
        result = filter_tool_defs(defs, "minimal")
        names = [d["function"]["name"] for d in result]
        assert names == ["bing_search"]
