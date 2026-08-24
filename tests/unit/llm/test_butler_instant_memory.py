"""ButlerAgent 实时强记忆捕获单元测试：正则命中稳定事实即入库的纯函数逻辑。

只测 _instant_memory_entries（确定性规则，不触网、不依赖配置），
覆盖捕获、归属、防误捕获（否定/弱宾语/lore 泄漏/AI 自述）三类行为。
"""
from ev.llm.butler_agent import _instant_memory_entries


def _turns(*contents):
    """把 [role, content] 列表包装成轮次字典列表。"""
    return [
        {"role": role, "content": content} for role, content in contents
    ]


class TestInstantCapture:
    def test_preference_danmaku(self):
        entries = _instant_memory_entries(
            _turns(("user", "[弹幕@蓝奶] 我喜欢喝草莓奶茶")))
        assert len(entries) == 1
        assert entries[0]["content"] == "蓝奶喜欢喝草莓奶茶"
        assert entries[0]["user"] == "蓝奶"
        assert entries[0]["name"] == "蓝奶的喜好"

    def test_age(self):
        entries = _instant_memory_entries(_turns(("user", "我今年22岁")))
        assert entries[0]["content"] == "chao今年22岁"

    def test_relation(self):
        entries = _instant_memory_entries(
            _turns(("user", "[弹幕@小明] 小美是我姐姐")))
        assert entries[0]["content"] == "小明的姐姐是小美"
        assert entries[0]["description"] == "core/实体记忆：小美 是 小明 的 姐姐"

    def test_location_work(self):
        entries = _instant_memory_entries(
            _turns(("user", "[弹幕@小明] 我在上海工作")))
        assert entries[0]["content"] == "小明在上海工作/上学"

    def test_birthday(self):
        entries = _instant_memory_entries(
            _turns(("user", "[弹幕@小明] 我的生日是3月14日")))
        assert entries[0]["content"] == "小明的生日是3月14日"

    def test_name(self):
        entries = _instant_memory_entries(
            _turns(("user", "[弹幕@小明] 我叫王小明")))
        assert entries[0]["content"] == "小明的名字是王小明"

    def test_pet(self):
        entries = _instant_memory_entries(
            _turns(("user", "[弹幕@小明] 我养了只猫")))
        assert entries[0]["content"] == "小明养了猫"

    def test_plain_user_input_owner_default(self):
        # 无弹幕前缀的非 AI 轮次归主播（默认用户）
        entries = _instant_memory_entries(_turns(("user", "我在深圳生活")))
        assert entries[0]["user"] == "chao"


class TestInstantSkip:
    def test_negation_not_captured_as_like(self):
        # 「我不喜欢」绝不能误捕获为喜好
        entries = _instant_memory_entries(
            _turns(("user", "[弹幕@蓝奶] 我不喜欢香菜")))
        assert len(entries) == 1
        assert entries[0]["content"] == "蓝奶不喜欢香菜"

    def test_weak_object_skipped(self):
        # 代词/泛指宾语不是稳定事实
        assert _instant_memory_entries(
            _turns(("user", "[弹幕@小明] 我喜欢这个"))) == []

    def test_lore_leak_skipped(self):
        # 世界观讨论（命中 lore 词库）不入用户记忆
        assert _instant_memory_entries(
            _turns(("user", "[弹幕@小明] 流萤和萨姆是什么关系"))) == []

    def test_ai_self_turn_skipped(self):
        # AI 自述轮次由批量提取处理，不做实时捕获
        assert _instant_memory_entries(
            _turns(("assistant", "我喜欢收集蓝箱子"))) == []

    def test_empty_turns(self):
        assert _instant_memory_entries([]) == []

    def test_one_capture_per_turn(self):
        # 一句只捕获最强的一条，避免同句碎片
        entries = _instant_memory_entries(
            _turns(("user", "[弹幕@小明] 我叫王小明，我今年22岁")))
        assert len(entries) == 1
