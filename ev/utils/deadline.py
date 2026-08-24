"""统一 bounded 执行原语（对标 Hermes agent/deadline.py 精简落地）。

3.17：asyncio.wait_for 的超时由事件循环定时器驱动——事件循环被同步调用
阻塞（如 sf.read 同步 IO、弹幕 burst）时，asyncio 超时全部失效。本模块
用 daemon threading.Timer 驱动超时（线程池调度，不受事件循环阻塞影响），
配合 asyncio 侧超时做双保险，解决"主循环阻塞时超时失效"问题。

用法：
    result = await run_bounded_async(coro, timeout=5.0, label="转写")
    if result.timed_out:
        ...
    value = result.value
"""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Optional

# 平台等待原语可接受的最大超时（threading.Lock.acquire/Thread.join 的
# 绝对时间戳在大值时可能溢出 time_t，一年语义上等价"不限时"）
_MAX_SAFE_TIMEOUT_S = 31_536_000.0  # 365 天


class DeadlineExpired(TimeoutError):
    """本层强制的截止时间到期（区别于 provider 传输超时）。"""

    def __init__(self, label: str, timeout_s: float) -> None:
        super().__init__(f"deadline expired after {timeout_s:.1f}s: {label}")
        self.label = label
        self.timeout_s = timeout_s


@dataclass(frozen=True)
class BoundedResult:
    """受限执行的结果；timed_out 为超时语义（异常原样向调用方抛出）。"""
    timed_out: bool
    value: Any
    elapsed_s: float
    timeout_s: Optional[float]
    label: str

    def raise_if_timed_out(self) -> Any:
        """超时抛 DeadlineExpired，否则返回 value。"""
        if self.timed_out:
            raise DeadlineExpired(self.label, float(self.timeout_s or 0.0))
        return self.value


def clamp_timeout(timeout: Optional[float]) -> Optional[float]:
    """归一化超时：None/非正/NaN → None（不限时）；超大值截断防平台溢出。"""
    if timeout is None:
        return None
    try:
        value = float(timeout)
    except (TypeError, ValueError):
        return None
    if value != value or value <= 0:  # NaN 或非正
        return None
    return min(value, _MAX_SAFE_TIMEOUT_S)


async def run_bounded_async(
    awaitable: Awaitable[Any],
    timeout: Optional[float],
    *,
    label: str = "operation",
) -> BoundedResult:
    """在墙钟截止时间下等待 awaitable（超时不依赖事件循环定时器）。

    - 正常完成：返回 BoundedResult(timed_out=False, value=...)；
      操作自身异常原样抛出（调用方保留既有错误处理）；
    - 超时：底层任务被取消并**放弃**（不等待取消完成——取消屏蔽路径
      如 httpcore 初始化可能永久卡死）；返回 BoundedResult(timed_out=True)；
    - 事件循环被同步阻塞时：daemon threading.Timer 到点仍会触发
      deadline future（call_soon_threadsafe 排队），loop 恢复后立即超时；
      未阻塞时 asyncio.wait 自带 timeout 先触发（双保险）；
    - timeout 为 None/非正：不限时等待。
    """
    timeout_s = clamp_timeout(timeout)
    start = time.monotonic()
    if timeout_s is None:
        value = await awaitable
        return BoundedResult(
            timed_out=False, value=value,
            elapsed_s=time.monotonic() - start, timeout_s=None, label=label)

    task = asyncio.ensure_future(awaitable)
    loop = asyncio.get_running_loop()
    deadline: "asyncio.Future[None]" = loop.create_future()

    def _mark_expired() -> None:
        if not deadline.done():
            deadline.set_result(None)

    def _expire_from_thread() -> None:
        # Timer 线程跨线程安全唤醒 loop（loop 阻塞时排队，恢复即执行）
        loop.call_soon_threadsafe(_mark_expired)

    timer = threading.Timer(timeout_s, _expire_from_thread)
    timer.daemon = True
    timer.start()
    try:
        done, _ = await asyncio.wait(
            {task, deadline},
            return_when=asyncio.FIRST_COMPLETED,
            timeout=timeout_s,  # asyncio 侧双保险
        )
        if task in done:
            if not deadline.done():
                deadline.cancel()
            value = await task
            return BoundedResult(
                timed_out=False, value=value,
                elapsed_s=time.monotonic() - start, timeout_s=timeout_s, label=label)
        task.cancel()  # 超时：取消并放弃底层任务（不 await 取消完成）
        return BoundedResult(
            timed_out=True, value=None,
            elapsed_s=time.monotonic() - start, timeout_s=timeout_s, label=label)
    except asyncio.CancelledError:
        # 调用方取消我们：底层任务一并取消并放弃，避免泄漏
        task.cancel()
        raise
    finally:
        # 正常完成/取消路径取消挂起 Timer，防止测试结束后 fire 报 loop closed
        timer.cancel()
