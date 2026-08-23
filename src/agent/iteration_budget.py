"""迭代次数预算：限制单任务的 LLM 调用轮数，防止失控循环。

与 TokenBudget（token 用量）正交：这是"轮数"预算。耗尽时调用方
必须做一次不带工具的总结调用（宽限收尾），避免硬断时用户拿不到结论。

对标 Hermes agent/iteration_budget.py：
- consume()：每次 LLM 调用消耗 1 额度，已满返回 False；
- refund()：工具调用被取消/失败时可返还 1 额度；
- 线程安全（LLM 子线程/回调可能并发访问）。
"""

from __future__ import annotations

import threading


class IterationBudget:
    """线程安全迭代计数（父/子 Agent 各持一份独立预算）。"""

    def __init__(self, max_iterations: int = 30) -> None:
        self.max_iterations = max(max_iterations, 1)
        self._used = 0
        self._lock = threading.Lock()

    def consume(self) -> bool:
        """消耗一次调用额度；已满返回 False（调用方停止常规循环）。"""
        with self._lock:
            if self._used >= self.max_iterations:
                return False
            self._used += 1
            return True

    def refund(self) -> None:
        """工具调用被取消/失败时返还一次额度。"""
        with self._lock:
            if self._used > 0:
                self._used -= 1

    def reset(self) -> None:
        """清零计数（Agent 实例复用时与历史清空对称）。"""
        with self._lock:
            self._used = 0

    @property
    def used(self) -> int:
        with self._lock:
            return self._used

    @property
    def remaining(self) -> int:
        with self._lock:
            return max(0, self.max_iterations - self._used)
