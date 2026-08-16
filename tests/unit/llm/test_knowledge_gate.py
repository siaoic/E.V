"""知识库信号闸门单元测试（对标 E.V_REFACTOR.md §5.2 知识库信号闸门 13 用例）。

闸门判定是纯函数逻辑，不依赖外部服务，可直接单测。
"""
import pytest

from src.llm.knowledge.gate import KnowledgeGate
from src.llm.knowledge.loader import load_knowledge


@pytest.fixture
def gate() -> KnowledgeGate:
    """带完整实体表的闸门（与 KnowledgeService._ensure_loaded 一致）。"""
    g = KnowledgeGate()
    kb = load_knowledge()
    g.register_entities([kw for fact in kb.facts for kw in fact.keywords])
    return g


class TestChitChat:
    """纯闲聊 → 零注入。"""

    @pytest.mark.parametrize("msg", [
        "哈哈",
        "。。。",
        "www",
        "emmm",
        "🤣🤣",
        "~ ~ ~",
    ])
    def test_pure_chitchat_zero(self, gate, msg):
        assert gate.level(msg) == 0

    def test_empty_string(self, gate):
        assert gate.level("") == 0
        assert gate.level("   ") == 0


class TestShortMessage:
    """过短且无实体 → 零注入（但短身份问题是剧情意图，仍全层）。"""

    def test_short_no_entity(self, gate):
        assert gate.level("好") == 0
        assert gate.level("嗯嗯") == 0

    def test_short_identity_question(self, gate):
        # 「你是谁」虽短，但属剧情意图 → 全层
        assert gate.level("你是谁") == 2

    def test_short_with_entity_passes(self, gate):
        # 过短但含知识库实体（怕死/恐惧）→ 放行走检索
        assert gate.level("你怕死吗") >= 1
        assert gate.level("你最大的恐惧是什么") >= 1


class TestEntityHit:
    """实体别名命中 → L0a+L0b（level 1）。"""

    @pytest.mark.parametrize("msg", [
        "neuro是谁",
        "讲讲evil",
        "swarm是什么",
        "vedal是谁",
        "你怕死吗",
        "你最大的恐惧是什么",
    ])
    def test_entity_hit(self, gate, msg):
        assert gate.level(msg) == 1


class TestPlotIntent:
    """剧情意图（身份 / 关系 / 经历 / 世界观）→ 全层（level 2）。"""

    @pytest.mark.parametrize("msg", [
        "你是谁",
        "她跟谁什么关系",
        "你来自哪里",
        "说说你的故事",
        "讲讲你的背景",
        "介绍一下你",
        "为什么你是星核猎手",
        "你认识卡芙卡吗",
        "who are you",
        "where are you from",
    ])
    def test_plot_intent(self, gate, msg):
        assert gate.level(msg) == 2


class TestMediumMessage:
    """中等长度无实体 → 默认 L0a（level 1，安全兜底；无匹配内容不实际注入）。"""

    def test_medium_default(self, gate):
        assert gate.level("今天天气真好，出门散步很舒服") == 1


class TestEntityFilter:
    """实体注册过滤：短词 / 高频虚词不进入实体表。"""

    def test_stop_words_filtered(self):
        g = KnowledgeGate()
        g.register_entities(["我", "你", "是", "neuro", "的"])
        assert "我" not in g._entities
        assert "你" not in g._entities
        assert "neuro" in g._entities

    def test_one_char_filtered(self):
        g = KnowledgeGate()
        g.register_entities(["梦", "好"])
        assert g._entities == []

    def test_case_insensitive(self):
        g = KnowledgeGate()
        g.register_entities(["SAM", "s.a.m."])
        assert "sam" in g._entities


class TestShouldInject:
    def test_should_inject_chitchat_false(self, gate):
        assert not gate.should_inject("哈哈")

    def test_should_inject_intent_true(self, gate):
        assert gate.should_inject("你是谁")
