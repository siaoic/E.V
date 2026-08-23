"""MemoryManager / StreamingContextScrubber 单元测试（对标 Hermes memory_manager）。

覆盖 3.5 升级验证点：
- 外部 Provider 上限（1 内置 + 至多 1 外部，第 2 个外部被拒）；
- prefetch 召回硬超时（MEMORY_GATE_TIMEOUT，超时熔断不拖垮对话）；
- <memory-context> 围栏构建与 StreamingContextScrubber 剥除（含跨 chunk）；
- build_system_prompt 会话级冻结（frozen snapshot，写盘不刷新 prompt）；
- sync_all 后台单 worker 串行写回（主循环不阻塞）。
"""
import asyncio
import time
import threading
from types import SimpleNamespace

import pytest

from src.utils import config
from src.llm.memory.manager import (
    MemoryManager,
    StreamingContextScrubber,
    _MEMORY_FENCE_START,
    _MEMORY_FENCE_END,
)
from src.llm.memory.provider import MemoryProvider


# ---------- 替身 ----------

class FakeProvider(MemoryProvider):
    """可配置延迟/静态块/内置标记的假 Provider。"""

    def __init__(self, name, *, available=True, builtin=False,
                 block="", delay=0.0):
        self._name = name
        self._available = available
        self._builtin = builtin
        self._block = block
        self._delay = delay
        self.prefetch_calls = []
        self.sync_calls = []
        self.sync_event = threading.Event()
        self.shutdown_called = False

    @property
    def name(self):
        return self._name

    @property
    def is_builtin(self):
        return self._builtin

    def is_available(self):
        return self._available

    def system_prompt_block(self):
        return self._block

    def prefetch(self, query, *, session_id=""):
        self.prefetch_calls.append((query, session_id))
        if self._delay:
            time.sleep(self._delay)
        return f"{self._name}:{query}"

    def sync_turn(self, user_content, assistant_content, *,
                  session_id="", messages=None):
        self.sync_calls.append((user_content, assistant_content))
        self.sync_event.set()

    def shutdown(self):
        self.shutdown_called = True


# ---------- StreamingContextScrubber ----------

class TestScrubber:
    def test_plain_text_unchanged(self):
        assert StreamingContextScrubber.strip("你好世界") == "你好世界"

    def test_inline_fence_stripped(self):
        text = f"前 {_MEMORY_FENCE_START}记忆内容{_MEMORY_FENCE_END} 后"
        assert StreamingContextScrubber.strip(text) == "前  后"

    def test_multiple_fences(self):
        text = (f"{_MEMORY_FENCE_START}A{_MEMORY_FENCE_END}"
                f"中间{_MEMORY_FENCE_START}B{_MEMORY_FENCE_END}")
        assert StreamingContextScrubber.strip(text) == "中间"

    def test_unclosed_fence_drops_tail(self):
        text = f"前 {_MEMORY_FENCE_START}没有闭合"
        assert StreamingContextScrubber.strip(text) == "前 "

    def test_cross_chunk_state(self):
        scrubber = StreamingContextScrubber()
        chunk1 = f"开头{_MEMORY_FENCE_START}记忆"
        assert scrubber.scrub(chunk1) == "开头"  # 围栏内剩余丢弃
        chunk2 = f"还在记忆里{_MEMORY_FENCE_END}结尾"
        assert scrubber.scrub(chunk2) == "结尾"  # 跨 chunk 剥除

    def test_cross_chunk_unclosed(self):
        scrubber = StreamingContextScrubber()
        assert scrubber.scrub(f"{_MEMORY_FENCE_START}abc") == ""
        assert scrubber.scrub("def") == ""


# ---------- 注册上限 ----------

class TestProviderLimit:
    def test_second_external_rejected(self):
        mgr = MemoryManager()
        mgr.add_provider(FakeProvider("ext-a"))
        mgr.add_provider(FakeProvider("ext-b"))  # 第 2 个外部 → 拒绝
        assert [p.name for p in mgr.providers] == ["ext-a"]

    def test_builtin_does_not_occupy_slot(self):
        mgr = MemoryManager()
        mgr.add_provider(FakeProvider("memu", builtin=True))
        mgr.add_provider(FakeProvider("ext-a"))
        mgr.add_provider(FakeProvider("ext-b"))  # builtin 不占名额，仍只允许 1 外部
        assert [p.name for p in mgr.providers] == ["memu", "ext-a"]

    def test_duplicate_name_rejected(self):
        mgr = MemoryManager()
        mgr.add_provider(FakeProvider("memu", builtin=True))
        mgr.add_provider(FakeProvider("memu", builtin=True))
        assert len(mgr.providers) == 1

    def test_unavailable_rejected(self):
        mgr = MemoryManager()
        mgr.add_provider(FakeProvider("off", available=False))
        assert mgr.providers == []


# ---------- 召回超时与围栏 ----------

class TestPrefetch:
    @pytest.mark.asyncio
    async def test_prefetch_timeout_skips_slow_backend(self, monkeypatch):
        # config.cfg 的 MEMORY_GATE_TIMEOUT 是只读 property，整体替换 cfg 便于注入
        monkeypatch.setattr(config, "cfg", SimpleNamespace(MEMORY_GATE_TIMEOUT=1.0))
        mgr = MemoryManager()
        mgr.add_provider(FakeProvider("slow", delay=3.0))
        start = time.monotonic()
        result = await mgr.prefetch_all("你好")
        elapsed = time.monotonic() - start
        assert result == ""          # 超时熔断，不注入慢后端结果
        assert elapsed < 2.0         # 1s 超时内返回

    @pytest.mark.asyncio
    async def test_prefetch_fence_wraps_results(self, monkeypatch):
        monkeypatch.setattr(config, "cfg", SimpleNamespace(MEMORY_GATE_TIMEOUT=0.0))
        mgr = MemoryManager()
        mgr.add_provider(FakeProvider("ext-a", block=""))
        result = await mgr.prefetch_all("你好")
        assert result.startswith(_MEMORY_FENCE_START)
        assert result.endswith(_MEMORY_FENCE_END)
        assert "ext-a:你好" in result

    @pytest.mark.asyncio
    async def test_prefetch_provider_exception_isolated(self, monkeypatch):
        monkeypatch.setattr(config, "cfg", SimpleNamespace(MEMORY_GATE_TIMEOUT=0.0))

        class BoomProvider(FakeProvider):
            def prefetch(self, query, *, session_id=""):
                raise RuntimeError("boom")

        mgr = MemoryManager()
        mgr.add_provider(BoomProvider("boom", builtin=True))
        mgr.add_provider(FakeProvider("ok"))
        result = await mgr.prefetch_all("你好")
        assert "ok:你好" in result  # 单个失败不阻塞其他

    def test_is_trivial_prompt(self):
        assert MemoryProvider.is_trivial_prompt("")
        assert MemoryProvider.is_trivial_prompt("哈哈")
        assert MemoryProvider.is_trivial_prompt("   ")
        assert not MemoryProvider.is_trivial_prompt("我喜欢喝咖啡")


# ---------- 冻结快照 ----------

class TestFrozenPrompt:
    def test_build_system_prompt_frozen(self):
        provider = FakeProvider("memu", builtin=True, block="静态段A")
        mgr = MemoryManager()
        mgr.add_provider(provider)
        first = mgr.build_system_prompt()
        assert first == "静态段A"

        provider._block = "静态段B"  # 写盘刷新不应改变已冻结的 prompt
        second = mgr.build_system_prompt()
        assert second == "静态段A"
        assert first is second  # 同一对象（同一字符串对象）

    def test_empty_blocks_join_empty(self):
        mgr = MemoryManager()
        mgr.add_provider(FakeProvider("a", block=""))
        mgr.add_provider(FakeProvider("b", block="  "))
        assert mgr.build_system_prompt() == ""


# ---------- 后台写回 ----------

class TestSyncAll:
    @pytest.mark.asyncio
    async def test_sync_all_background_worker(self):
        mgr = MemoryManager()
        provider = FakeProvider("ext-a")
        mgr.add_provider(provider)
        await mgr.sync_all("用户话", "回复话")
        assert provider.sync_calls == []  # 入队后主循环立即返回
        assert provider.sync_event.wait(timeout=2.0)  # 后台 worker 落地
        assert provider.sync_calls == [("用户话", "回复话")]

    @pytest.mark.asyncio
    async def test_shutdown_all_drains_and_closes(self):
        mgr = MemoryManager()
        provider = FakeProvider("ext-a")
        mgr.add_provider(provider)
        await mgr.sync_all("用户话", "回复话")
        await mgr.shutdown_all()
        assert provider.shutdown_called
        await mgr.sync_all("a", "b")  # 关闭后入队被忽略
        assert len(provider.sync_calls) <= 1
