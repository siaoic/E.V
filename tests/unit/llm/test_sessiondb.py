"""3.7 SessionDB（会话落库 + FTS5 检索）单元测试。

验证：写入→中文子串命中（搜"流萤"命中含"流萤"的历史消息，对标文档验证节）；
tool 行不落库；四模式检索；开关关闭时 get_session_db 返回 None（零开销旁路）。
"""
import pytest
from types import SimpleNamespace

import src.utils.config as config
from src.llm.sessiondb import SessionDB, get_session_db, record_turn_queued


@pytest.fixture
def db(tmp_path):
    """独立 SessionDB 实例（直连临时库，不经单例）。"""
    session = SessionDB(tmp_path / "state.db")
    session.record_turn("sess-1", "user", "你好，今天聊点流萤的话题", "2026-08-01T10:00:00")
    session.record_turn("sess-1", "assistant", "流萤确实很可爱呢", "2026-08-01T10:00:05")
    session.record_turn("sess-1", "tool", "{\"cmd\": \"ls\"}", "2026-08-01T10:00:10")
    session.record_turn("sess-2", "user", "今天晚饭吃什么", "2026-08-02T12:00:00")
    session.record_turn("sess-2", "assistant", "", "2026-08-02T12:00:01")  # 空内容不落
    session.flush()
    yield session
    session.close()


class TestWriteFilter:
    def test_tool_and_empty_not_indexed(self, db):
        rows = db.search(mode="SCROLL", session_id="sess-1", limit=50)["results"]
        roles = {r["role"] for r in rows}
        assert roles == {"user", "assistant"}
        assert all(r["content"] for r in rows)


class TestDiscover:
    def test_chinese_substring_hit(self, db):
        """中文 2 字词（trigram 不支持的长度）经 LIKE 兜底命中。"""
        results = db.search(query="流萤", limit=10)["results"]
        assert len(results) >= 2  # user 与 assistant 两条都含"流萤"

    def test_long_keyword_via_fts(self, db):
        results = db.search(query="今天晚饭", limit=10)["results"]
        assert len(results) == 1
        assert results[0]["session_id"] == "sess-2"

    def test_empty_query_returns_nothing(self, db):
        assert db.search(query="  ", limit=10)["results"] == []


class TestOtherModes:
    def test_read_by_id(self, db):
        rows = db.search(mode="READ", msg_id=1)["results"]
        assert len(rows) == 1
        assert rows[0]["id"] == 1

    def test_scroll_before_ts(self, db):
        rows = db.search(mode="SCROLL", session_id="sess-1",
                         before_ts="2026-08-01T10:00:05", limit=10)["results"]
        assert len(rows) == 1
        assert rows[0]["ts"] == "2026-08-01T10:00:00"

    def test_scroll_unknown_session(self, db):
        assert db.search(mode="SCROLL", session_id="nope", limit=10)["results"] == []

    def test_browse_session(self, db):
        rows = db.search(mode="BROWSE", session_id="sess-2", limit=10)["results"]
        assert len(rows) == 1
        assert rows[0]["content"] == "今天晚饭吃什么"

    def test_limit_clamped(self, db):
        rows = db.search(mode="SCROLL", session_id="sess-1", limit=0)["results"]
        assert len(rows) <= 50


class TestSingletonGate:
    def test_disabled_returns_none(self, monkeypatch, tmp_path):
        monkeypatch.setattr(config, "cfg", SimpleNamespace(
            DATA_ROOT=str(tmp_path), ENABLE_SESSION_SEARCH=False))
        assert get_session_db() is None

    def test_record_turn_queued_noop_when_disabled(self, monkeypatch, tmp_path):
        monkeypatch.setattr(config, "cfg", SimpleNamespace(
            DATA_ROOT=str(tmp_path), ENABLE_SESSION_SEARCH=False))
        record_turn_queued("s", "user", "不落库", "")  # 不应抛异常
        assert not (tmp_path / "state.db").exists()

    def test_enabled_singleton(self, monkeypatch, tmp_path):
        monkeypatch.setattr(config, "cfg", SimpleNamespace(
            DATA_ROOT=str(tmp_path), ENABLE_SESSION_SEARCH=True))
        assert get_session_db() is not None
        assert get_session_db() is get_session_db()  # 单例
        get_session_db().close()
