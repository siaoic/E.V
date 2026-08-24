"""会话租约（3.10）单测：同 session 串行 / 异 session 并行 / 关闭时放行。"""
import asyncio

import pytest

from ev.kernel.turn_lease import TurnLeaseRegistry, session_turn_gate


class TestTurnLeaseRegistry:
    def test_same_session_serializes(self):
        """同 session 两个请求必须串行（第二个等第一个释放）。"""
        async def case():
            reg = TurnLeaseRegistry(queue_timeout=5.0)
            order = []
            running = asyncio.Event()
            enter = asyncio.Event()

            async def task(name):
                ok = await reg.acquire("s1")
                if not ok:
                    order.append(f"{name}:fail")
                    return
                try:
                    order.append(f"{name}:in")
                    if name == "a":
                        running.set()
                        await enter.wait()
                finally:
                    reg.release("s1")
                    order.append(f"{name}:out")

            t1 = asyncio.create_task(task("a"))
            await running.wait()
            t2 = asyncio.create_task(task("b"))
            await asyncio.sleep(0.05)
            # b 还没进去，a 还在持有
            assert order == ["a:in"]
            enter.set()
            await asyncio.gather(t1, t2)
            assert order == ["a:in", "a:out", "b:in", "b:out"]

        asyncio.run(case())

    def test_different_sessions_parallel(self):
        """异 session 完全并行，不互相等待。"""
        async def case():
            reg = TurnLeaseRegistry(queue_timeout=5.0)
            done = []

            async def task(name, sid, delay):
                ok = await reg.acquire(sid)
                assert ok
                try:
                    await asyncio.sleep(delay)
                    done.append(name)
                finally:
                    reg.release(sid)

            await asyncio.gather(task("a", "s1", 0.2), task("b", "s2", 0.05))
            assert done == ["b", "a"]

        asyncio.run(case())

    def test_queue_timeout_drops_new(self):
        """同 session 排队超时 → 新请求被丢弃（返回 False）。"""
        async def case():
            reg = TurnLeaseRegistry(queue_timeout=0.05)
            release = asyncio.Event()

            async def holder():
                assert await reg.acquire("s1")
                await release.wait()
                reg.release("s1")

            t1 = asyncio.create_task(holder())
            await asyncio.sleep(0.02)
            ok = await reg.acquire("s1")
            assert ok is False  # 超时丢弃
            release.set()
            await t1

        asyncio.run(case())

    def test_clear_releases_all(self):
        reg = TurnLeaseRegistry()
        reg.clear()
        assert not reg._locks


class TestSessionTurnGate:
    def test_disabled_by_default(self):
        """默认开关关闭：gate 直接放行（行为等同现状）。"""
        import ev.kernel.turn_lease as tl

        async def case():
            async with session_turn_gate("s1") as ok:
                return ok

        assert asyncio.run(case()) is True
        assert not tl.turn_lease_enabled()

    def test_enabled_serializes_gate(self):
        """开关开启：gate 包裹的对话按 session 串行。"""
        import ev.kernel.turn_lease as tl

        async def case():
            orig = tl.turn_lease_enabled
            tl.turn_lease_enabled = lambda: True
            try:
                order = []

                async def run(name):
                    async with session_turn_gate("s1") as ok:
                        assert ok
                        order.append(name)
                        await asyncio.sleep(0.05)

                await asyncio.gather(run("a"), run("b"))
                assert order == ["a", "b"]
            finally:
                tl.turn_lease_enabled = orig

        asyncio.run(case())
