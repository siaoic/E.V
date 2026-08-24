"""抖动退避（3.13）单测：数值分布 / 递增 / 上限 / 去相关。"""
import random

from ev.utils.retry_utils import jittered_backoff


class TestJitteredBackoff:
    def test_in_range(self):
        """结果落在 [delay, delay + jitter_ratio*delay] 内。"""
        for attempt in (1, 2, 3):
            delay = jittered_backoff(attempt, base_delay=5.0, jitter_ratio=0.5)
            base = min(5.0 * (2 ** (attempt - 1)), 120.0)
            assert base <= delay <= base * 1.5

    def test_monotonic_growth(self):
        """未触顶时随 attempt 指数增长。"""
        d1 = jittered_backoff(1, base_delay=1.0, max_delay=120.0, jitter_ratio=0.0)
        d2 = jittered_backoff(2, base_delay=1.0, max_delay=120.0, jitter_ratio=0.0)
        assert d2 == 2.0 and d1 == 1.0

    def test_max_delay_cap(self):
        """超过上限后恒定在上限。"""
        d = jittered_backoff(10, base_delay=1.0, max_delay=60.0, jitter_ratio=0.0)
        assert d == 60.0

    def test_decorrelated_jitter(self):
        """同参数多次调用抖动值不同（去相关，防惊群）。"""
        values = {jittered_backoff(1, base_delay=1.0, jitter_ratio=0.9)
                  for _ in range(50)}
        assert len(values) > 1

    def test_zero_base_delay(self):
        """base_delay<=0 时直接返回 max_delay。"""
        assert jittered_backoff(1, base_delay=0.0, max_delay=30.0,
                                jitter_ratio=0.0) == 30.0
