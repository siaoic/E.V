"""IterationBudget（3.1）单元测试：额度消耗 / 返还 / 清零 / 线程安全。"""
import threading

from src.agent.iteration_budget import IterationBudget


class TestConsume:
    def test_consume_until_exhausted(self):
        b = IterationBudget(max_iterations=3)
        assert [b.consume() for _ in range(3)] == [True, True, True]
        assert b.consume() is False  # 满后拒绝
        assert b.used == 3
        assert b.remaining == 0

    def test_max_iterations_at_least_one(self):
        assert IterationBudget(max_iterations=0).max_iterations == 1
        assert IterationBudget(max_iterations=-5).max_iterations == 1

    def test_default_cap(self):
        assert IterationBudget().max_iterations == 30


class TestRefund:
    def test_refund_returns_slot(self):
        b = IterationBudget(max_iterations=2)
        assert b.consume() and b.consume()
        assert b.consume() is False
        b.refund()
        assert b.used == 1
        assert b.consume() is True  # 返还后恢复可消费

    def test_refund_never_negative(self):
        b = IterationBudget(max_iterations=2)
        for _ in range(5):
            b.refund()
        assert b.used == 0
        assert b.remaining == 2


class TestReset:
    def test_reset_clears_usage(self):
        b = IterationBudget(max_iterations=2)
        assert b.consume() and b.consume()
        b.reset()
        assert b.used == 0
        assert b.remaining == 2
        assert b.consume() is True

    def test_remaining_after_partial_use(self):
        b = IterationBudget(max_iterations=5)
        b.consume()
        b.consume()
        assert b.remaining == 3


class TestThreadSafety:
    def test_concurrent_consume_exact_cap(self):
        """多线程并发消费：成功总数恰为上限，无超卖。"""
        cap = 200
        b = IterationBudget(max_iterations=cap)
        results = []

        def worker():
            for _ in range(100):
                results.append(b.consume())

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert sum(results) == cap
        assert b.consume() is False
