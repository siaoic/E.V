"""Agent Token 预算单元测试：消耗 / 重置 / 满载判定 / 模型窗口映射。"""
from src.agent.budget import TokenBudget, model_to_max_context


class TestTokenBudget:
    def test_initial_used_zero(self):
        b = TokenBudget(100)
        assert b.used == 0

    def test_consume(self):
        b = TokenBudget(100)
        b.consume(30)
        assert b.used == 30

    def test_consume_negative_ignored(self):
        b = TokenBudget(100)
        b.consume(-5)
        assert b.used == 0

    def test_reset(self):
        b = TokenBudget(100)
        b.consume(80)
        b.reset()
        assert b.used == 0

    def test_is_full_at_limit(self):
        b = TokenBudget(100)
        b.consume(100)
        assert b.is_full()

    def test_is_full_at_trigger_line(self):
        # 默认触发线 = 上限 × 0.75
        b = TokenBudget(100)
        b.consume(75)
        assert b.is_full()
        b2 = TokenBudget(100)
        b2.consume(74)
        assert not b2.is_full()

    def test_is_full_with_estimate(self):
        b = TokenBudget(100)
        b.consume(60)
        assert b.is_full(estimate=20)      # 60+20 = 80 ≥ 75 触发线 → 满载
        assert not b.is_full(estimate=10)  # 60+10 = 70 < 75 → 未满

    def test_not_full_under_limit(self):
        b = TokenBudget(100)
        b.consume(50)
        assert not b.is_full()

    def test_custom_trigger_ratio(self):
        b = TokenBudget(100, trigger_ratio=0.5)
        assert b.trigger_threshold == 50
        b.consume(50)
        assert b.is_full()

    def test_remaining(self):
        b = TokenBudget(100)
        b.consume(30)
        assert b.remaining == 70
        b.consume(100)
        assert b.remaining == 0  # 不出现负数

    def test_add_estimates_by_chars(self):
        b = TokenBudget(100)
        b.add("a" * 25)          # 25 字符 ÷ 2.5 字符/token = 10
        assert b.used == 10
        b.add("")                # 空文本不累计
        assert b.used == 10

    def test_model_window_mapping(self):
        b = TokenBudget(model_name="glm-4.7-flash")
        assert b.max_tokens == 128_000
        assert b.trigger_threshold == int(128_000 * 0.75)

    def test_unknown_model_falls_back_default(self):
        b = TokenBudget(model_name="no-such-model")
        assert b.max_tokens == 128_000


class TestModelToMaxContext:
    def test_exact_match(self):
        assert model_to_max_context("deepseek-v4-pro") == 1_000_000

    def test_wildcard_match(self):
        assert model_to_max_context("glm-4.7-flash") == 128_000  # glm-*
        assert model_to_max_context("qwen-old") == 32_000        # qwen-*

    def test_empty_and_unknown(self):
        assert model_to_max_context("") == 128_000
        assert model_to_max_context("foo") == 128_000
