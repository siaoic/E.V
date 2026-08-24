"""技能系统链路单元测试：扫描 / 注入段 / 工具注册 / 加载 / 资源 / 意图匹配 / 使用统计。

全部本地操作，不触网。技能使用统计文件重定向到 tmp_path，避免污染真实
data/skill_usage.json（模块级 _USAGE_PATH 在 import 时求值，需显式覆盖）。
"""
import json

import pytest

from plugins.builtin.tools import get_merged_tools
from plugins.builtin.tools import skills as skills_mod
from plugins.builtin.tools.skill_loader import _load_skill, _read_skill_resource
from plugins.builtin.tools.skills import get_skill_manager
from ev.llm.llm_brain import LLMBrain


@pytest.fixture(autouse=True)
def _usage_in_tmp(tmp_path, monkeypatch):
    """技能使用统计文件重定向到临时目录，并从临时文件重载统计缓存。"""
    monkeypatch.setattr("plugins.builtin.tools.skills._USAGE_PATH",
                        str(tmp_path / "skill_usage.json"))
    mgr = get_skill_manager()
    mgr._usage = mgr._load_usage()


class TestScanAndInject:
    """技能扫描 + 主对话注入段 + 工具注册。"""

    def test_scan_contains_daily_diary(self):
        names = {s.name for s in get_skill_manager().skills}
        assert "daily-diary" in names

    def test_render_prompt_section(self):
        section = get_skill_manager().render_prompt_section()
        assert "可用技能" in section
        assert "load_skill" in section
        assert "daily-diary" in section

    def test_merged_tools_register_skill_tools(self):
        names = {t["function"]["name"] for t in get_merged_tools()}
        assert "load_skill" in names
        assert "read_skill_resource" in names


class TestLoadSkill:
    """load_skill / read_skill_resource 行为（含越界防护）。"""

    @pytest.mark.asyncio
    async def test_load_skill_returns_md_and_resources(self):
        text = await _load_skill("daily-diary")
        assert "Skill: daily-diary" in text
        assert "references" in text  # 捆绑资源清单提示

    @pytest.mark.asyncio
    async def test_load_skill_unknown_name(self):
        text = await _load_skill("no_such_skill")
        assert "不存在" in text

    @pytest.mark.asyncio
    async def test_read_resource_blog(self):
        text = await _read_skill_resource(
            "daily-diary", "references/blogs/2026-08-03-周记.md")
        assert text and "Skill: daily-diary" in text

    @pytest.mark.asyncio
    async def test_read_resource_traversal_rejected(self):
        text = await _read_skill_resource("daily-diary", "../README.md")
        assert "越界" in text or "超出" in text


class TestIntentMatch:
    """本地意图匹配（§3.5.3）：零依赖，无需 LLM。"""

    def test_match_diary(self):
        hit = get_skill_manager().match_intent("帮我写一篇今天的日记")
        assert hit is not None and hit.name == "daily-diary"

    def test_no_match_returns_none(self):
        assert get_skill_manager().match_intent("今天天气怎么样") is None


class TestSkillIntentHint:
    """match_intent 接入主对话的预判提示（LLMBrain._skill_intent_hint）。"""

    def test_match_diary_hint(self):
        hint = LLMBrain(mcp=None)._skill_intent_hint("帮我写一篇今天的日记")
        assert hint and "daily-diary" in hint and "load_skill" in hint

    def test_no_match_hint_empty(self):
        assert LLMBrain(mcp=None)._skill_intent_hint("今天吃什么好呢") == ""


class TestUsageStats:
    """技能使用统计：record_usage → 落盘 → 进化引擎感知。"""

    def test_record_and_query(self):
        mgr = get_skill_manager()
        before = mgr.usage_of("daily-diary")
        mgr.record_usage("daily-diary")
        after = mgr.usage_of("daily-diary")
        assert after["loads"] == (before["loads"] + 1 if before else 1)

    def test_usage_persisted_to_file(self):
        mgr = get_skill_manager()
        mgr.record_usage("daily-diary")
        assert skills_mod._USAGE_PATH
        with open(skills_mod._USAGE_PATH, encoding="utf-8") as f:
            data = json.load(f)
        assert "daily-diary" in data

    def test_usage_section_for_evolution(self):
        mgr = get_skill_manager()
        mgr.record_usage("daily-diary")
        section = mgr.usage_section()
        assert "daily-diary" in section and "loads=" in section
