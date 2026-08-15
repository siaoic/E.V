"""429 限流响应头解析：服务端建议的等待秒数。"""

import time


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
