"""全局急停（3.13）单测：哨兵生命周期 / fail-safe / 高危工具拦截。"""
import json

from ev.kernel import estop


def _patch_sentinel(monkeypatch, tmp_path):
    """把哨兵路径重定向到临时目录（不污染真实 DATA_ROOT）。"""
    sentinel = tmp_path / "ESTOP"
    monkeypatch.setattr(estop, "sentinel_path", lambda: sentinel)
    return sentinel


class TestEstopLifecycle:
    def test_not_engaged_by_default(self, monkeypatch, tmp_path):
        _patch_sentinel(monkeypatch, tmp_path)
        assert not estop.is_engaged()

    def test_engage_disengage(self, monkeypatch, tmp_path):
        sentinel = _patch_sentinel(monkeypatch, tmp_path)
        assert estop.engage("测试急停") == sentinel
        assert sentinel.exists()
        assert estop.is_engaged()
        state = estop.get_state()
        assert state["reason"] == "测试急停"
        assert estop.disengage()
        assert not sentinel.exists()
        assert not estop.is_engaged()
        assert not estop.disengage()  # 已解除，再次返回 False

    def test_corrupt_sentinel_still_engaged(self, monkeypatch, tmp_path):
        """内容损坏/不可读的哨兵文件仍算急停（fail-safe）。"""
        sentinel = _patch_sentinel(monkeypatch, tmp_path)
        sentinel.write_text("{corrupt", encoding="utf-8")
        assert estop.is_engaged()
        assert estop.get_state() == {"reason": None, "engaged_at": None}


class TestEstopBlock:
    def test_blocked_high_risk_when_engaged(self, monkeypatch, tmp_path):
        """急停生效时高危工具被拦，只读工具不受影响。"""
        _patch_sentinel(monkeypatch, tmp_path)
        estop.engage()
        assert estop.is_blocked("run_shell")
        assert estop.is_blocked("write_diary")
        assert estop.is_blocked("get_weather")
        assert not estop.is_blocked("get_current_time")  # 只读工具放行
        assert not estop.is_blocked("session_search")

    def test_not_blocked_when_disengaged(self, monkeypatch, tmp_path):
        _patch_sentinel(monkeypatch, tmp_path)
        assert not estop.is_engaged()
        assert not estop.is_blocked("run_shell")  # 无哨兵恒放行

    def test_not_blocked_when_disabled(self, monkeypatch, tmp_path):
        """开关关闭时即使有哨兵也不拦（行为与现状一致）。"""
        _patch_sentinel(monkeypatch, tmp_path)
        monkeypatch.setattr(estop, "estop_enabled", lambda: False)
        estop.engage()
        assert not estop.is_blocked("run_shell")

    def test_unknown_tool_not_blocked(self, monkeypatch, tmp_path):
        """拦截集外的工具不受急停影响。"""
        _patch_sentinel(monkeypatch, tmp_path)
        estop.engage()
        assert not estop.is_blocked("load_skill")
