"""复读防护（3.13）单测：复读样本命中 / 正常文本不命中 / 开关。"""
from src.utils.repetition_guard import (
    is_repetition_dominated, repetition_guard_enabled,
)


def _repeat_line(line: str, count: int) -> str:
    """构造 count 行相同文本（每行含换行），用于复读样本。"""
    return (line + "\n") * count


class TestRepetitionGuard:
    def test_repetition_dominated_hits(self):
        """长片段内单行重复 5 次且覆盖过半 → 判复读。"""
        # 每行 120 字符 × 5 行 = 600 字符，单行覆盖 600*120/600 = 120 ≥ 50%
        text = _repeat_line("今天天气真好啊我们来聊聊天吧" * 10, 5)
        assert is_repetition_dominated(text)

    def test_window_repetition_hits(self):
        """非行对齐的固定窗口重复同样命中（慢路径）。"""
        # 72 字块重复 8 次 = 576 字符 ≥ 400；60 字窗口按 72 周期重复 ≥5 次
        block = "这是一个会反复出现的测试片段块内容" * 4
        text = block * 8
        assert is_repetition_dominated(text)

    def test_clean_text_not_hit(self):
        """正常长文本不触发（无 60+ 字逐字重复）。"""
        # 400+ 字符的不同句子拼接：窗口内容各异，不构成逐字重复
        text = "".join(
            f"第{i}句话，今天天气不错，我们来聊聊第{i}个话题吧。"
            for i in range(20))
        assert len(text) >= 400
        assert not is_repetition_dominated(text)

    def test_short_text_not_hit(self):
        """短片段（<400 字符）不检查，直接放行。"""
        assert not is_repetition_dominated("哈哈哈哈哈")
        assert not is_repetition_dominated(_repeat_line("x" * 80, 3))  # 3 行 < 400

    def test_non_string_fail_open(self):
        """非字符串/空输入 fail-open。"""
        assert not is_repetition_dominated("")
        assert not is_repetition_dominated(None)

    def test_disabled_by_flag(self, monkeypatch):
        """开关关闭时恒不拦截。"""
        monkeypatch.setattr(
            "src.utils.repetition_guard.repetition_guard_enabled",
            lambda: False)
        text = _repeat_line("今天天气真好啊我们来聊聊天吧" * 10, 5)
        assert not is_repetition_dominated(text)

    def test_enabled_by_default(self):
        """开关默认开启（正常文本不触发，无行为影响）。"""
        assert repetition_guard_enabled()
