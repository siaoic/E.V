"""开播就绪检查（3.15）单测：探针聚合 / 容错 / 开关。"""
import asyncio
from types import SimpleNamespace

from src.core.readiness import (
    check_readiness, readiness_check_enabled, warn_failures,
)


def _healthy_runtime():
    """构造全健康 fake runtime（内部状态全部就绪，不发网络请求）。"""
    alive_thread = SimpleNamespace(is_alive=lambda: True)
    svc = SimpleNamespace(_running=True,
                          _services=[SimpleNamespace(_bili_thread=alive_thread)])
    return SimpleNamespace(
        tts=SimpleNamespace(_ready=True),
        vts=SimpleNamespace(_ws_open=lambda: True),
        bili_svc=svc,
        mm=SimpleNamespace(_service=object()),
        mcp=SimpleNamespace(is_enabled=False, mcp_servers={}, transports={}),
        stt_engine=None,
    )


def _run(coro):
    return asyncio.run(coro)


class TestCheckReadiness:
    def test_all_healthy(self):
        report = _run(check_readiness(_healthy_runtime()))
        assert report["ok"] is True
        names = [c["name"] for c in report["checks"]]
        assert names == ["tts", "vts", "danmaku", "memory", "mcp", "asr"]
        assert all(c["ok"] for c in report["checks"])

    def test_component_none_is_ok_or_fail(self):
        """组件缺失按项健康/不健康处理（asr 未启用 = 健康，bili 缺失 = 不健康）。"""
        runtime = _healthy_runtime()
        runtime.bili_svc = None
        report = _run(check_readiness(runtime))
        assert not report["ok"]
        by_name = {c["name"]: c for c in report["checks"]}
        assert by_name["danmaku"]["ok"] is False
        assert by_name["asr"]["ok"] is True  # 未启用 STT 视为不适用（健康）

    def test_vts_disconnected_fails(self):
        runtime = _healthy_runtime()
        runtime.vts = SimpleNamespace(_ws_open=lambda: False)
        report = _run(check_readiness(runtime))
        by_name = {c["name"]: c for c in report["checks"]}
        assert by_name["vts"]["ok"] is False
        assert not report["ok"]

    def test_mcp_partial_servers_fails(self):
        runtime = _healthy_runtime()
        runtime.mcp = SimpleNamespace(
            is_enabled=True, mcp_servers={"a": 1, "b": 2}, transports={"a": object()})
        report = _run(check_readiness(runtime))
        by_name = {c["name"]: c for c in report["checks"]}
        assert by_name["mcp"]["ok"] is False

    def test_probe_exception_tolerated(self, monkeypatch):
        """探针抛异常按该项不健康处理，不向调用方抛。"""
        runtime = _healthy_runtime()

        def boom(_runtime):
            raise RuntimeError("探针内部故障")

        async def down(_url):
            return False, "mock down"

        monkeypatch.setattr("src.core.readiness._tts_probe", boom)
        # 内部探针异常触发活体复核：mock 为失败，避免真实服务在线改判健康
        monkeypatch.setattr("src.core.readiness._probe_http", down)
        report = _run(check_readiness(runtime))
        by_name = {c["name"]: c for c in report["checks"]}
        assert by_name["tts"]["ok"] is False
        # 活体复核失败会覆盖 detail（异常本身已被探针层容错）
        assert by_name["tts"]["detail"] == "mock down"

    def test_disabled_returns_empty(self, monkeypatch):
        """READINESS_CHECK 关闭时返回空报告（不打扰启动）。"""
        monkeypatch.setattr("src.core.readiness.readiness_check_enabled",
                            lambda: False)
        report = _run(check_readiness(_healthy_runtime()))
        assert report == {"ok": True, "checks": []}

    def test_enabled_by_default(self):
        assert readiness_check_enabled()

    def test_warn_failures_no_raise(self):
        """告警函数对全健康与含失败的报告均不抛异常。"""
        warn_failures({"ok": True, "checks": [{"name": "tts", "ok": True,
                                               "detail": "ok"}]})
        warn_failures({"ok": False, "checks": [{"name": "tts", "ok": False,
                                                "detail": "down"}]})
