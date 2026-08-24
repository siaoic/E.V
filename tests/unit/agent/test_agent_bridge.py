"""主对话与 sub-agent 桥单元测试：_should_delegate 判定 + maybe_delegate 回退。

兼容性核心：AGENT_ENABLED=false（默认）时 maybe_delegate 直接返回 None，
主对话不进入委派分支，行为与现状 100% 一致。
"""
import pytest
from types import SimpleNamespace

from ev.agent.bridge import MainChatSubAgentBridge, _DELEGATE_KEYWORDS


def _make_runtime(*, agent_enabled=False, llm_base="http://x",
                  llm_key="k", agent_model="m", llm_model="m"):
    """构造带 cfg 的假 runtime，控制 Agent 开关与 LLM 配置。"""
    cfg = SimpleNamespace(
        AGENT_ENABLED=agent_enabled,
        LLM_BASE_URL=llm_base,
        LLM_API_KEY=llm_key,
        AGENT_MODEL=agent_model,
        LLM_MODEL=llm_model,
    )
    return SimpleNamespace(cfg=cfg, proactive=None, tts=None,
                           face=None, sub=None)


class TestShouldDelegate:
    """判定规则：Agent 开关 + LLM 配置 + 关键词命中。"""

    def test_agent_disabled_returns_false(self):
        """AGENT_ENABLED=false（默认）→ 不委派（兼容性核心保证）。"""
        bridge = MainChatSubAgentBridge()
        runtime = _make_runtime(agent_enabled=False)
        for keyword in _DELEGATE_KEYWORDS:
            assert not bridge._should_delegate(
                f"帮我{keyword}xxx", runtime)

    def test_agent_enabled_no_keyword_returns_false(self):
        """Agent 开启但未命中关键词 → 不委派（普通对话走 converse）。"""
        bridge = MainChatSubAgentBridge()
        runtime = _make_runtime(agent_enabled=True)
        assert not bridge._should_delegate("你好", runtime)
        assert not bridge._should_delegate("今天天气不错", runtime)

    def test_agent_enabled_keyword_hit_returns_true(self):
        """Agent 开启 + 关键词命中 + 配置完整 → 委派。"""
        bridge = MainChatSubAgentBridge()
        runtime = _make_runtime(agent_enabled=True)
        for keyword in _DELEGATE_KEYWORDS:
            assert bridge._should_delegate(
                f"帮我{keyword}xxx", runtime), f"关键词 {keyword} 应触发委派"

    def test_llm_config_missing_returns_false(self):
        """Agent 开启但 LLM 配置缺失 → 不委派（避免启动必然失败的任务）。"""
        bridge = MainChatSubAgentBridge()
        runtime = _make_runtime(
            agent_enabled=True, llm_base="", llm_key="")
        assert not bridge._should_delegate("帮我调研一下", runtime)

    def test_empty_text_returns_false(self):
        """空文本 → 不委派。"""
        bridge = MainChatSubAgentBridge()
        runtime = _make_runtime(agent_enabled=True)
        assert not bridge._should_delegate("", runtime)
        assert not bridge._should_delegate("   ", runtime)


class TestMaybeDelegateCompat:
    """maybe_delegate 回退路径：未命中/Agent 关闭/空文本时返回 None。

    这些场景下主对话走原 converse 路径，行为与现状一致（兼容性保证）。
    """

    @pytest.mark.asyncio
    async def test_agent_disabled_returns_none(self):
        """AGENT_ENABLED=false → maybe_delegate 直接返回 None。"""
        bridge = MainChatSubAgentBridge()
        runtime = _make_runtime(agent_enabled=False)
        result = await bridge.maybe_delegate(
            "帮我调研一下直播热度", runtime)
        assert result is None

    @pytest.mark.asyncio
    async def test_no_keyword_returns_none(self):
        """未命中关键词 → 返回 None。"""
        bridge = MainChatSubAgentBridge()
        runtime = _make_runtime(agent_enabled=True)
        result = await bridge.maybe_delegate("你好", runtime)
        assert result is None

    @pytest.mark.asyncio
    async def test_empty_text_returns_none(self):
        """空文本 → 返回 None。"""
        bridge = MainChatSubAgentBridge()
        runtime = _make_runtime(agent_enabled=True)
        result = await bridge.maybe_delegate("", runtime)
        assert result is None

    @pytest.mark.asyncio
    async def test_llm_config_missing_returns_none(self):
        """LLM 配置缺失 → 返回 None（不启动必然失败的后台任务）。"""
        bridge = MainChatSubAgentBridge()
        runtime = _make_runtime(
            agent_enabled=True, llm_base="", llm_key="")
        result = await bridge.maybe_delegate("帮我调研一下", runtime)
        assert result is None


class TestListRecent:
    """DelegationQueue.list_recent：读最近 N 条任务。"""

    def test_empty_db_returns_empty_list(self, tmp_path, monkeypatch):
        """空库（无任务）→ 空列表。"""
        monkeypatch.setattr(
            "ev.agent.async_delegation.delegate_backend_enabled",
            lambda: True)
        from ev.agent.async_delegation import DelegationQueue
        q = DelegationQueue(tmp_path / "delegation.db")
        assert q.list_recent(10) == []

    def test_returns_recent_n(self, tmp_path, monkeypatch):
        """入队 5 条 → list_recent(3) 返回最近 3 条（按 id 倒序）。"""
        monkeypatch.setattr(
            "ev.agent.async_delegation.delegate_backend_enabled",
            lambda: True)
        from ev.agent.async_delegation import DelegationQueue
        q = DelegationQueue(tmp_path / "delegation.db")
        ids = [q.enqueue(f"任务{i}") for i in range(5)]
        recent = q.list_recent(3)
        assert len(recent) == 3
        # 按 id 倒序：最近入队的在前
        assert [r["id"] for r in recent] == list(reversed(ids))[:3]
        # 字段完整
        assert all("status" in r and "task" in r and "attempts" in r
                   for r in recent)

    def test_limit_clamped_to_max(self, tmp_path, monkeypatch):
        """limit > 200 截断到 200；limit < 1 截断到 1。"""
        monkeypatch.setattr(
            "ev.agent.async_delegation.delegate_backend_enabled",
            lambda: True)
        from ev.agent.async_delegation import DelegationQueue
        q = DelegationQueue(tmp_path / "delegation.db")
        q.enqueue("任务")
        # limit=999 截断到 200（不会报错）
        result = q.list_recent(999)
        assert len(result) == 1
        # limit=0 或负数截断到 1
        assert len(q.list_recent(0)) == 1
        assert len(q.list_recent(-5)) == 1
