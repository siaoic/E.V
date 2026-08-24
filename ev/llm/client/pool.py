"""异步 OpenAI 客户端全局池：按 (事件循环, base_url, api_key, 超时桶) 复用实例。

factory.py 统一了构造逻辑，但每次调用仍 new 一个 AsyncOpenAI，httpx 连接池
各占一份——auxiliary 每次调用 new + finally close 尤其浪费（连接池刚建即弃）。
池化后同一事件循环内同 (base_url, api_key, 超时桶) 的调用复用同一实例与连接池，
TLS/握手开销归零。

设计要点（避坑）：
- 跨事件循环绝不复用：Windows Proactor 上跨循环复用 httpx client 会报
  "Event loop is closed" 或静默等待（见 project_memory 教训）。cache key 含
  loop_id，不同循环各自持实例。
- 超时分桶：实例级 timeout 取桶值（向上取整到 30s 档），同桶复用；所有异步
  调用方均有外层 asyncio.wait_for 兜底（见 butler._complete / auxiliary /
  evolution.call_llm_json），实例 timeout 取大值不影响实际超时语义。
- 池化实例不可 close：调用方禁止 await client.close()（会关掉共享连接池）。
  auxiliary 的 finally close 已删除；其余调用方（butler/evolution/prompt_evo）
  本就缓存到 self._client 不 close，行为不变。
- 进程退出由 atexit 统一清理（fail-open，不阻塞）。
"""
from __future__ import annotations

import atexit
import asyncio
import math
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from openai import AsyncOpenAI

_POOL_LOCK = threading.Lock()
_ASYNC_POOL: dict[tuple, "AsyncOpenAI"] = {}
# 同步池（OpenAI，brain / model_router 用）：按 (base_url, api_key, 超时桶) 复用
# 同步 httpx.Client 线程安全，多线程可共享；但 brain 已自缓存到 self.client，
# model_router 探测不频繁，池化收益有限——同步池保守起见暂不启用（保留接口）。
_SYNC_POOL: dict[tuple, "OpenAI"] = {}

# 超时分桶粒度（秒）：同桶复用，避免 45s/60s 各占一个实例
_BUCKET_SIZE = 30.0
# 无运行循环时的退化桶值（同步上下文调用场景）
_FALLBACK_BUCKET = 60.0


def _running_loop_id() -> int:
    """获取当前运行事件循环 id；不在 async 上下文返回 0（退化共享）。

    调用方在 async 方法内调本函数时，asyncio.get_running_loop() 能拿到当前
    循环；同步 def 被异步调用时同样在循环内（运行时仍挂在该循环）。
    """
    try:
        return id(asyncio.get_running_loop())
    except RuntimeError:
        return 0  # 无运行循环（如解释器启动期），退化到 0 号桶


def _timeout_bucket(timeout: float) -> float:
    """超时向上取整到 30s 档：30/60/90/120...，同桶复用。

    实例级 timeout 取桶值（≥ 调用方请求值），所有异步调用方依赖外层
    asyncio.wait_for 兜底实际超时，桶值偏大不影响语义。
    """
    if timeout <= 0:
        return _FALLBACK_BUCKET
    return math.ceil(timeout / _BUCKET_SIZE) * _BUCKET_SIZE


def get_pooled_async_client(
    *,
    api_key: str,
    base_url: str = "",
    timeout: float = 60.0,
    max_retries: int = 2,
) -> "AsyncOpenAI":
    """按 (loop, base_url, api_key, 超时桶) 复用 AsyncOpenAI 实例。

    同一事件循环内同配置的调用复用同一实例（含其 httpx 连接池），
    跨循环各自独立实例。调用方禁止 await client.close()。
    """
    from openai import AsyncOpenAI

    from ev.llm.client.factory import _normalize_endpoint

    key, url = _normalize_endpoint(api_key, base_url)
    bucket = _timeout_bucket(timeout)
    cache_key = (_running_loop_id(), url or "", key, bucket)
    with _POOL_LOCK:
        client = _ASYNC_POOL.get(cache_key)
        if client is None:
            client = AsyncOpenAI(
                api_key=key,
                base_url=url,
                timeout=bucket,
                max_retries=max_retries,
            )
            _ASYNC_POOL[cache_key] = client
        return client


def get_pooled_sync_client(
    *,
    api_key: str,
    base_url: str = "",
    timeout: float = 120.0,
    max_retries: int = 2,
) -> "OpenAI":
    """按 (base_url, api_key, 超时桶) 复用同步 OpenAI 实例。

    同步 httpx.Client 线程安全，多线程可共享。当前 brain / model_router
    已各自缓存，池化收益有限，保留接口供后续接入。
    """
    from openai import OpenAI

    from ev.llm.client.factory import _normalize_endpoint

    key, url = _normalize_endpoint(api_key, base_url)
    bucket = _timeout_bucket(timeout)
    cache_key = (url or "", key, bucket)
    with _POOL_LOCK:
        client = _SYNC_POOL.get(cache_key)
        if client is None:
            client = OpenAI(
                api_key=key,
                base_url=url,
                timeout=bucket,
                max_retries=max_retries,
            )
            _SYNC_POOL[cache_key] = client
        return client


def close_all() -> None:
    """进程退出清理：关闭所有池化客户端（fail-open，不抛异常）。

    atexit 时通常无运行事件循环，异步 close 用 asyncio.run 起新循环执行；
    若已有循环（手动调 close_all），asyncio.run 报 RuntimeError 被 except 吞掉，
    由 OS 回收资源（fail-open）。
    """
    with _POOL_LOCK:
        async_clients = list(_ASYNC_POOL.values())
        sync_clients = list(_SYNC_POOL.values())
        _ASYNC_POOL.clear()
        _SYNC_POOL.clear()
    for client in sync_clients:
        try:
            client.close()
        except Exception:
            pass
    for client in async_clients:
        try:
            close_coro = client.close()
            if asyncio.iscoroutine(close_coro):
                asyncio.run(close_coro)
        except Exception:
            pass


atexit.register(close_all)
