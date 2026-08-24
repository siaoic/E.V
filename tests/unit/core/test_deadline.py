"""deadline（3.17）单元测试：bounded 执行原语 + 超时归一化。

关键场景：事件循环被同步调用阻塞时，asyncio 定时器失效，
daemon threading.Timer 路径必须仍能触发超时（对标文档 3.17 验证节）。
"""
import asyncio
import time

import pytest

from ev.utils.deadline import (
    BoundedResult, DeadlineExpired, clamp_timeout, run_bounded_async,
)


class TestClampTimeout:
    def test_none_and_invalid(self):
        assert clamp_timeout(None) is None
        assert clamp_timeout(0) is None
        assert clamp_timeout(-1) is None
        assert clamp_timeout(float("nan")) is None
        assert clamp_timeout("abc") is None

    def test_valid_values(self):
        assert clamp_timeout(1.5) == 1.5
        assert clamp_timeout("2.5") == 2.5

    def test_huge_capped(self):
        # 超过平台等待原语安全上限的按上限截断（防 time_t 溢出）
        assert clamp_timeout(10 ** 9) == 31_536_000.0


class TestBoundedResult:
    def test_raise_when_timed_out(self):
        r = BoundedResult(True, None, 0.1, 0.05, "x")
        with pytest.raises(DeadlineExpired) as ei:
            r.raise_if_timed_out()
        assert "x" in str(ei.value)
        assert ei.value.timeout_s == 0.05

    def test_return_value_when_ok(self):
        r = BoundedResult(False, "ok", 0.01, None, "x")
        assert r.raise_if_timed_out() == "ok"


class TestRunBoundedAsync:
    @pytest.mark.asyncio
    async def test_normal_completion(self):
        result = await run_bounded_async(asyncio.sleep(0.01, result=42), 5.0, label="sleep")
        assert result.timed_out is False
        assert result.value == 42

    @pytest.mark.asyncio
    async def test_no_timeout_waits(self):
        result = await run_bounded_async(asyncio.sleep(0.01, result=1), None, label="x")
        assert result.timed_out is False
        assert result.value == 1

    @pytest.mark.asyncio
    async def test_timeout_triggers(self):
        result = await run_bounded_async(asyncio.sleep(1.0), 0.05, label="slow")
        assert result.timed_out is True
        assert result.value is None
        assert result.elapsed_s < 0.5  # 未等到底层任务完成

    @pytest.mark.asyncio
    async def test_operation_exception_propagates(self):
        async def boom():
            raise ValueError("boom")
        with pytest.raises(ValueError, match="boom"):
            await run_bounded_async(boom(), 1.0, label="boom")

    @pytest.mark.asyncio
    async def test_caller_cancellation_propagates(self):
        async def never():
            await asyncio.sleep(5.0)
        with pytest.raises(asyncio.CancelledError):
            task = asyncio.ensure_future(run_bounded_async(never(), 5.0, label="x"))
            await asyncio.sleep(0.01)
            task.cancel()
            await task

    @pytest.mark.asyncio
    async def test_timeout_survives_blocked_event_loop(self):
        """事件循环被同步调用阻塞时超时仍生效（Timer 路径）。

        阻塞 150ms 期间 asyncio 定时器无法调度；恢复后应立刻判定超时，
        而不是等到底层 sleep(0.5) 结束。
        """
        pending = asyncio.ensure_future(
            run_bounded_async(asyncio.sleep(0.5), 0.05, label="blocked"))
        await asyncio.sleep(0.01)  # 让 run_bounded_async 进入等待状态
        time.sleep(0.15)  # 同步阻塞事件循环
        result = await pending
        assert result.timed_out is True
        assert result.elapsed_s < 0.4  # 阻塞恢复后立即超时，非等完 0.5s
