"""网关会话与并发控制（升级项 3.10，对标 Hermes gateway/turn_lease.py）。

同一会话（session_id）的重入保护：同一观众的连续输入、或语音与键盘同时触发
时，按 session 排队串行进入 brain，避免并发处理同一会话。

- TurnLeaseRegistry：按 session_id 加锁；同 session 并发请求排队串行
  （排队超时则丢弃新请求，遵循"说话不被打断"）；异 session 完全并行。
- 开关 TURN_LEASE_ENABLED=0（默认关闭）：acquire() 直接放行（空实现），
  行为与现状完全一致；启用后仅在进入 brain 前显式 gate 的入口生效。
- 内存态注册表：进程重启自然失效，无需清理。
"""
from __future__ import annotations

import asyncio
import threading
import time
from contextlib import asynccontextmanager
from typing import Any, Optional

from ev.utils import config


def turn_lease_enabled() -> bool:
    """3.10 总开关：关闭时 acquire/release 为空实现，行为等同现状。"""
    try:
        return bool(config.cfg.TURN_LEASE_ENABLED)
    except Exception:
        return False


class TurnLeaseRegistry:
    """按 session_id 串行化的会话租约注册表（线程安全）。"""

    def __init__(self, queue_timeout: float = 10.0) -> None:
        # session_id → asyncio.Lock（惰性创建，进程内存态）
        self._locks: dict[str, asyncio.Lock] = {}
        self._guard = threading.Lock()
        self._queue_timeout = queue_timeout

    def _lock_for(self, session_id: str) -> asyncio.Lock:
        with self._guard:
            lock = self._locks.get(session_id)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[session_id] = lock
            return lock

    async def acquire(self, session_id: str) -> bool:
        """获取会话租约：同 session 排队串行，等待超时返回 False（丢弃新请求）。"""
        if not session_id:
            return True
        lock = self._lock_for(session_id)
        try:
            await asyncio.wait_for(lock.acquire(), timeout=self._queue_timeout)
            return True
        except asyncio.TimeoutError:
            return False

    def release(self, session_id: str) -> None:
        """释放会话租约（必须与成功的 acquire 成对调用）。"""
        if not session_id:
            return
        lock = self._locks.get(session_id)
        if lock is not None and lock.locked():
            lock.release()

    def clear(self) -> None:
        """清空全部租约（进程退出/会话重置时调用）。"""
        with self._guard:
            self._locks.clear()


@asynccontextmanager
async def session_turn_gate(session_id: str):
    """进入 brain 前的会话门：默认放行；启用时同 session 串行化。

    usage:
        async with session_turn_gate(session_id) as ok:
            if not ok:
                return  # 排队超时，丢弃该请求（不缓存）
            await converse(...)
    """
    if not turn_lease_enabled():
        yield True
        return
    reg = get_turn_registry()
    ok = await reg.acquire(session_id)
    try:
        yield ok
    finally:
        if ok:
            reg.release(session_id)


_registry: Optional[TurnLeaseRegistry] = None
_registry_guard = threading.Lock()


def get_turn_registry() -> TurnLeaseRegistry:
    """模块级单例（惰性创建）。"""
    global _registry
    with _registry_guard:
        if _registry is None:
            _registry = TurnLeaseRegistry()
        return _registry
