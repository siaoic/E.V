"""429 限流响应头解析：服务端建议的等待秒数。"""

import time
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class Retryable(Protocol):
    """外部 retry 工具（旧 utils.retry_utils.Retryable）的鸭子协议。

    旧代码从 `src.llm.client.retry import Retryable` 引用；迁移后这里以
    Protocol 形式提供同名类型占位，保证 `hasattr(mod, 'Retryable')` 为真。
    具体装饰器/重试逻辑仍在 ev.utils.retry_utils（非本 LLM 客户端职责）。
    """

    def __call__(self, func) -> Any: ...

    def with_config(self, **kwargs) -> "Retryable": ...


def _parse_retry_after(e) -> float:
    """从 429 异常的响应头解析服务端建议的等待秒数；解析失败返回 0。

    优先 Retry-After（秒数），其次 X-RateLimit-Reset（未来 Unix 时间戳，秒）。
    """
    try:
        headers = e.response.headers
    except Exception:
        return 0.0
    for name in ("Retry-After", "retry-after"):
        v = headers.get(name)
        if v:
            try:
                return max(0.0, float(v))
            except (TypeError, ValueError):
                pass
    for name in ("X-RateLimit-Reset", "x-ratelimit-reset"):
        v = headers.get(name)
        if v:
            try:
                ts = float(v)
                if ts > time.time():
                    return ts - time.time()
            except (TypeError, ValueError):
                pass
    return 0.0
