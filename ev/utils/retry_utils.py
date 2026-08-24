"""抖动退避工具（3.13，对标 Hermes agent/retry_utils.py）。

固定指数退避会在多个会话同时命中限流时形成"惊群"（同一时刻集体重试），
加抖动后各次重试的时间错开，缓解重试风暴。

用法：
    delay = jittered_backoff(attempt, base_delay=5.0, max_delay=120.0)
    await asyncio.sleep(delay)
"""

from __future__ import annotations

import random
import threading
import time

# 进程内抖动种子计数器：并发重试路径（多个后台线程同时退避）用锁保护，
# 保证每次调用得到的随机种子互不相同，从而抖动值彼此独立。
_jitter_counter = 0
_jitter_lock = threading.Lock()


def jittered_backoff(
    attempt: int,
    *,
    base_delay: float = 5.0,
    max_delay: float = 120.0,
    jitter_ratio: float = 0.5,
) -> float:
    """计算抖动指数退避延迟（秒）。

    参数：
        attempt: 第几次重试（1 起）。
        base_delay: 第 1 次重试的基础延迟（秒）。
        max_delay: 延迟上限（秒）。
        jitter_ratio: 抖动幅度占计算延迟的比例（0.5 = 均匀落在
            [0, 0.5*延迟]），去相关化并发重试。

    返回：
        min(base * 2^(attempt-1), max_delay) + 抖动
    """
    global _jitter_counter
    with _jitter_lock:
        _jitter_counter += 1
        tick = _jitter_counter

    exponent = max(0, attempt - 1)
    if exponent >= 63 or base_delay <= 0:
        delay = max_delay
    else:
        delay = min(base_delay * (2 ** exponent), max_delay)

    # 用 时间戳 ^ 计数器 做种子：即使系统时钟粗糙，同进程内各调用也互不相同
    seed = (time.time_ns() ^ (tick * 0x9E3779B9)) & 0xFFFFFFFF
    rng = random.Random(seed)
    jitter = rng.uniform(0, jitter_ratio * delay)
    return delay + jitter
