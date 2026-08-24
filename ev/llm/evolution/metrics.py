"""进化引擎轻量指标计数器（线程安全，进程内有效）。

供运行期观测各流程成功 / 失败 / 回滚次数（METRICS.snapshot() 可整体读取），
纯新增、无副作用，不影响任何业务行为。
"""

from __future__ import annotations

from collections import Counter
from threading import Lock


class EvolutionMetrics:
    """线程安全的累计计数器。"""

    def __init__(self) -> None:
        self._lock = Lock()
        self._counters: Counter[str] = Counter()

    def incr(self, key: str, by: int = 1) -> None:
        with self._lock:
            self._counters[key] += by

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return dict(self._counters)

    def reset(self) -> None:
        with self._lock:
            self._counters.clear()


METRICS = EvolutionMetrics()


# 事件名规范（统一前缀 evolution.）
EVENT_REVIEW_TRIGGERED = "evolution.review.triggered"
EVENT_REVIEW_FAILED = "evolution.review.failed"
EVENT_SKILL_SAVED = "evolution.skill.saved"
EVENT_SKILL_PATCHED = "evolution.skill.patched"
EVENT_SKILL_PATCH_REVERTED = "evolution.skill.patch.reverted"
EVENT_SKILL_MERGED = "evolution.skill.merged"
EVENT_SKILL_ARCHIVED = "evolution.skill.archived"
EVENT_TOPIC_ADDED = "evolution.topic.added"
EVENT_ADVICE_ADDED = "evolution.advice.added"
EVENT_ADVICE_REMOVED = "evolution.advice.removed"
EVENT_PROFILE_ADDED = "evolution.profile.added"
