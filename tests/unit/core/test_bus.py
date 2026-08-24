"""事件总线单元测试：订阅/取消/去重/广播/异常隔离。"""
import asyncio
import pytest

from ev.kernel.bus import EventBus, EV_USER_INPUT, EV_AI_REPLY


class TestSubscribe:
    def test_subscribe_and_emit(self):
        async def _inner():
            bus = EventBus()
            got = []
            async def handler(payload):
                got.append(payload)
            bus.subscribe(EV_USER_INPUT, handler)
            await bus.emit(EV_USER_INPUT, "hello")
            assert got == ["hello"]

        asyncio.run(_inner())

    def test_duplicate_subscribe_dedup(self):
        async def _inner():
            bus = EventBus()
            calls = []
            async def handler(payload):
                calls.append(payload)
            bus.subscribe(EV_AI_REPLY, handler)
            bus.subscribe(EV_AI_REPLY, handler)   # 重复订阅去重
            await bus.emit(EV_AI_REPLY, "x")
            assert len(calls) == 1

        asyncio.run(_inner())

    def test_unsubscribe(self):
        async def _inner():
            bus = EventBus()
            calls = []
            async def handler(payload):
                calls.append(payload)
            bus.subscribe(EV_USER_INPUT, handler)
            bus.unsubscribe(EV_USER_INPUT, handler)
            await bus.emit(EV_USER_INPUT, "y")
            assert calls == []

        asyncio.run(_inner())

    def test_unsubscribe_missing_silent(self):
        bus = EventBus()
        async def handler(payload):
            pass
        # 未订阅即取消：静默不报错
        bus.unsubscribe("never_subscribed", handler)


class TestEmit:
    def test_multiple_handlers_order(self):
        async def _inner():
            bus = EventBus()
            order = []
            async def h1(payload):
                order.append(1)
            async def h2(payload):
                order.append(2)
            bus.subscribe(EV_USER_INPUT, h1)
            bus.subscribe(EV_USER_INPUT, h2)
            await bus.emit(EV_USER_INPUT, None)
            assert order == [1, 2]

        asyncio.run(_inner())

    def test_handler_exception_isolated(self):
        """单个订阅者抛异常不影响其余订阅者接收。"""
        async def _inner():
            bus = EventBus()
            got = []
            async def bad(payload):
                raise RuntimeError("boom")
            async def good(payload):
                got.append(payload)
            bus.subscribe(EV_USER_INPUT, bad)
            bus.subscribe(EV_USER_INPUT, good)
            # 不向上抛异常
            await bus.emit(EV_USER_INPUT, "z")
            assert got == ["z"]

        asyncio.run(_inner())

    def test_no_handler_no_error(self):
        async def _inner():
            bus = EventBus()
            await bus.emit("nobody_listens", None)   # 无人订阅不报错

        asyncio.run(_inner())

    def test_different_events_isolated(self):
        async def _inner():
            bus = EventBus()
            got = []
            async def handler(payload):
                got.append(payload)
            bus.subscribe(EV_AI_REPLY, handler)
            await bus.emit(EV_USER_INPUT, "nope")
            assert got == []

        asyncio.run(_inner())


class TestWildcard:
    def test_pattern_matches_multi_events(self):
        async def _inner():
            bus = EventBus()
            got = []
            async def handler(payload):
                got.append(payload)
            bus.subscribe("speaking_*", handler)
            await bus.emit("speaking_start", 1)
            await bus.emit("speaking_end", 2)
            await bus.emit("ai_reply", 3)
            assert got == [1, 2]

        asyncio.run(_inner())

    def test_question_mark_single_char(self):
        async def _inner():
            bus = EventBus()
            got = []
            async def handler(payload):
                got.append(payload)
            bus.subscribe("ev_?rror", handler)
            await bus.emit("ev_error", 1)
            await bus.emit("ev_xrror", 2)   # 单字符通配
            await bus.emit("error", 3)
            assert got == [1, 2]

        asyncio.run(_inner())

    def test_on_any_catches_all(self):
        async def _inner():
            bus = EventBus()
            got = []
            async def handler(payload):
                got.append(payload)
            bus.on_any(handler)
            await bus.emit("user_input", "a")
            await bus.emit("whatever", "b")
            assert got == ["a", "b"]

        asyncio.run(_inner())

    def test_pattern_not_run_once_when_exact_also(self):
        """同一 handler 同时精确订阅 + 通配订阅，emit 只执行一次。"""
        async def _inner():
            bus = EventBus()
            calls = []
            async def handler(payload):
                calls.append(payload)
            bus.subscribe("speaking_start", handler)
            bus.subscribe("speaking_*", handler)
            await bus.emit("speaking_start", "x")
            assert calls == ["x"]

        asyncio.run(_inner())

    def test_unsubscribe_pattern(self):
        async def _inner():
            bus = EventBus()
            got = []
            async def handler(payload):
                got.append(payload)
            bus.subscribe("speaking_*", handler)
            bus.unsubscribe("speaking_*", handler)
            await bus.emit("speaking_end", 1)
            assert got == []

        asyncio.run(_inner())

    def test_on_off_aliases(self):
        async def _inner():
            bus = EventBus()
            got = []
            async def handler(payload):
                got.append(payload)
            bus.on(EV_AI_REPLY, handler)
            await bus.emit(EV_AI_REPLY, "ok")
            bus.off(EV_AI_REPLY, handler)
            await bus.emit(EV_AI_REPLY, "no")
            assert got == ["ok"]

        asyncio.run(_inner())


# ---- 新增: subscribers() 方法 & 全局单例共享 ----

async def _noop_a(ev): pass
async def _noop_b(ev): pass

def test_eventbus_subscribers_count():
    bus = EventBus()
    async def a(x): ...
    async def b(x): ...
    async def c(x): ...
    # 精确订阅
    bus.subscribe("user_input", a)
    bus.subscribe("user_input", b)
    bus.subscribe("ai_reply", c)
    # 通配订阅
    bus.subscribe("speaking_*", a)
    info = bus.subscribers()
    assert info["user_input"] == 2
    assert info["ai_reply"] == 1
    assert info["speaking_*"] == 1
    # unsubscribe
    bus.unsubscribe("user_input", a)
    assert bus.subscribers()["user_input"] == 1

def test_eventbus_global_singleton_with_local_is_shared():
    """Kernel 引用 self.event_bus = 全局 bus；确认两边 emit/sub 互通。"""
    # 模拟 Kernel 直接引用全局单例（是同一个对象引用）
    from ev.kernel.bus import bus as global_bus
    class MockKernel:
        event_bus = global_bus
    k = MockKernel()
    assert k.event_bus is global_bus  # 是同一个引用
    # 一边订阅、另一边 emit 可收到（用临时 handler）
    records = []
    async def handler(payload):
        records.append(payload)
    try:
        global_bus.subscribe("_test_bus_shared", handler)
        # kernel 侧 emit
        asyncio.run(k.event_bus.emit("_test_bus_shared", "hello"))
        assert records == ["hello"]
    finally:
        global_bus.unsubscribe("_test_bus_shared", handler)
