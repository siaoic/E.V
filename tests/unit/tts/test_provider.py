"""TTSProvider 抽象（3.14）单测：注册表解析 / GPTSoVITSProvider 包装转发。"""
import asyncio

import pytest

from src.tts import registry
from src.tts.provider import GPTSoVITSProvider, TTSProvider
from src.tts.registry import (
    DEFAULT_TTS_PROVIDER, get_provider, register_provider, resolve_provider,
)


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _clean_registry():
    """每个用例独立注册表，避免跨用例相互污染。"""
    registry._REGISTRY.clear()
    yield
    registry._REGISTRY.clear()


class _FakeEngine:
    """模拟 TTSEngine 最小接口，验证包装层转发。"""

    def __init__(self, ready=True):
        self._ready = ready
        self._client = object() if ready else None
        self._gen = 0
        self.spoken = []
        self.drained = False

    async def speak(self, text, sfx=""):
        self.spoken.append((text, sfx))

    async def drain(self):
        self.drained = True


class TestRegistry:
    def test_register_and_get(self):
        p = GPTSoVITSProvider()
        register_provider(p)
        assert get_provider("gpt-sovits") is p

    def test_case_insensitive_lookup(self):
        p = GPTSoVITSProvider()
        register_provider(p)
        assert get_provider("GPT-SOVITS") is p

    def test_last_write_wins(self):
        p1, p2 = GPTSoVITSProvider(), GPTSoVITSProvider()
        register_provider(p1)
        register_provider(p2)
        assert get_provider(DEFAULT_TTS_PROVIDER) is p2

    def test_get_missing_returns_none(self):
        assert get_provider("不存在的provider") is None

    def test_resolve_by_name_prefers_configured(self):
        p = GPTSoVITSProvider()
        register_provider(p)
        assert resolve_provider("gpt-sovits") is p

    def test_resolve_default_falls_back_first_available(self):
        # name 缺省且默认名未注册：回落首个 is_available 的 provider
        class AlwaysUp(TTSProvider):
            name = "up-provider"

            def is_available(self):
                return True

        class AlwaysDown(TTSProvider):
            name = "down-provider"

            def is_available(self):
                return False

        up = AlwaysUp()
        register_provider(up)            # 先注册可用者
        register_provider(AlwaysDown())  # 后注册不可用者（应被跳过）
        assert resolve_provider() is up

    def test_resolve_returns_default_when_none_available(self):
        # 全部不可用且未指定 name：返回调用方给的 default（不抛错）
        class AlwaysDown(TTSProvider):
            name = "down-provider"

            def is_available(self):
                return False

        register_provider(AlwaysDown())
        sentinel = object()
        assert resolve_provider(default=sentinel) is sentinel


class TestGPTSoVITSProvider:
    def test_not_available_without_engine(self):
        p = GPTSoVITSProvider()
        assert not p.is_available()

    def test_available_when_engine_ready(self):
        p = GPTSoVITSProvider(_FakeEngine(ready=True))
        assert p.is_available()

    def test_speak_forwards_to_engine(self):
        engine = _FakeEngine()
        p = GPTSoVITSProvider(engine)
        _run(p.speak("你好呀"))
        assert engine.spoken == [("你好呀", "")]

    def test_drain_forwards_to_engine(self):
        engine = _FakeEngine()
        p = GPTSoVITSProvider(engine)
        _run(p.drain())
        assert engine.drained

    def test_attach_engine_late(self):
        engine = _FakeEngine()
        p = GPTSoVITSProvider()
        p.attach_engine(engine)
        assert p.is_available()

    def test_speak_noop_without_engine(self):
        p = GPTSoVITSProvider()
        _run(p.speak("不会崩"))
        assert p._engine is None

    def test_voice_compatible_true(self):
        assert GPTSoVITSProvider().voice_compatible is True

    def test_name(self):
        assert GPTSoVITSProvider().name == "gpt-sovits"


class TestDefaultRegistration:
    def test_register_default_is_idempotent(self):
        p1 = registry.register_default_provider()
        p2 = registry.register_default_provider()
        assert p1 is p2
        assert get_provider(DEFAULT_TTS_PROVIDER) is p1
