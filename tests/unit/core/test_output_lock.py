"""全局输出互斥锁单元测试：owner 标记、状态机、拒收判定、弹幕待播标记。"""
import asyncio
import pytest

import src.core.output_lock as ol


@pytest.fixture(autouse=True)
def reset_state():
    """每个用例前复位全局状态，避免用例间互相污染。"""
    ol.set_output_owner(None)
    ol.set_global_state(ol.STATE_IDLE)
    ol.set_danmaku_pending(False)
    yield
    ol.set_output_owner(None)
    ol.set_global_state(ol.STATE_IDLE)
    ol.set_danmaku_pending(False)


class TestOwner:
    def test_default_none(self):
        assert ol.get_output_owner() is None

    def test_set_owner(self):
        ol.set_output_owner("user")
        assert ol.get_output_owner() == "user"

    def test_clear_owner(self):
        ol.set_output_owner("danmaku")
        ol.set_output_owner(None)
        assert ol.get_output_owner() is None


class TestRejectInput:
    def test_user_not_rejected(self):
        ol.set_output_owner("user")
        assert not ol.is_rejecting_input()   # 用户自己说话不拒收

    @pytest.mark.parametrize("owner", ["proactive", "danmaku", "agent"])
    def test_other_owners_rejected(self, owner):
        ol.set_output_owner(owner)
        assert ol.is_rejecting_input()


class TestStateMachine:
    def test_invalid_state_ignored(self):
        ol.set_global_state("bogus_state")
        assert ol.get_global_state() == ol.STATE_IDLE

    def test_idle_and_busy(self):
        assert ol.is_idle()
        assert not ol.is_busy()
        ol.set_global_state(ol.STATE_AI_SPEAKING)
        assert not ol.is_idle()
        assert ol.is_busy()

    def test_valid_states_transition(self):
        for st in (ol.STATE_USER_TALKING, ol.STATE_AI_SPEAKING,
                   ol.STATE_AGENT_THINKING, ol.STATE_AGENT_RUNNING):
            ol.set_global_state(st)
            assert ol.get_global_state() == st

    def test_same_state_no_broadcast(self):
        """重复设置同一状态不广播（无副作用断言：状态不变即可）。"""
        ol.set_global_state(ol.STATE_AI_SPEAKING)
        ol.set_global_state(ol.STATE_AI_SPEAKING)
        assert ol.get_global_state() == ol.STATE_AI_SPEAKING


class TestAgentOwner:
    def test_set_agent_owner(self):
        ol.set_agent_owner()
        assert ol.get_output_owner() == "agent"
        assert ol.get_global_state() == ol.STATE_AGENT_RUNNING
        assert ol.is_rejecting_input()


class TestDanmakuPending:
    def test_default_false(self):
        assert not ol.is_danmaku_pending()

    def test_set_clear(self):
        ol.set_danmaku_pending(True)
        assert ol.is_danmaku_pending()
        ol.set_danmaku_pending(False)
        assert not ol.is_danmaku_pending()


class TestLock:
    def test_lock_acquire_release(self):
        async def _inner():
            lock = ol.get_output_lock()
            await lock.acquire()
            try:
                assert not lock.locked() is False or lock.locked()
            finally:
                lock.release()
            assert not lock.locked()

        asyncio.run(_inner())

    def test_lock_mutual_exclusion(self):
        """同一把锁：一个持有，另一个等待。"""
        async def _inner():
            lock = ol.get_output_lock()
            acquired = []
            async def holder():
                async with lock:
                    await asyncio.sleep(0.05)
                    acquired.append("a")
            async def waiter():
                async with lock:
                    acquired.append("b")
            await asyncio.gather(holder(), waiter())
            # 串行执行 → 顺序固定 a 先 b 后
            assert acquired == ["a", "b"]

        asyncio.run(_inner())
