"""知识库加载 / 召回 / 格式化单元测试。

用项目真实 data/knowledge 数据验证：分层结构、召回排序、注入格式、
字符预算截断（不触网、不依赖 LLM）。
"""
import re

from src.llm.knowledge import KnowledgeService, load_knowledge
from src.llm.knowledge.format import format_for_injection
from src.llm.knowledge.loader import _split_lore_paragraphs
from src.llm.knowledge.recall import KnowledgeRecall


class TestLoadKnowledge:
    """数据层：金字塔四层均能加载且非空。"""

    def test_layers_loaded(self):
        kb = load_knowledge()
        assert len(kb.curated) >= 5      # L0a：5-10 张卡片
        assert len(kb.facts) >= 20       # L0b：20-30 条事实
        assert len(kb.lore) >= 10        # L0c+L1：角色亲历 + 世界观
        # 亲历 / 世界观均存在
        perspectives = {b.perspective for b in kb.lore}
        assert "first_person" in perspectives
        assert "third_person" in perspectives

    def test_curated_card_has_pattern(self):
        kb = load_knowledge()
        for card in kb.curated:
            assert isinstance(card.pattern, re.Pattern)

    def test_fact_fields(self):
        kb = load_knowledge()
        for fact in kb.facts:
            assert fact.id
            assert fact.keywords
            assert fact.answer
            assert 0 < fact.confidence <= 1


class TestLoreSplit:
    def test_split_by_heading(self):
        text = "## 关于身世\n第一段\n## 关于战场\n第二段\n"
        blocks = _split_lore_paragraphs(text)
        assert len(blocks) == 2
        assert blocks[0]["topic"] == "关于身世"
        assert blocks[1]["topic"] == "关于战场"

    def test_no_heading_single_block(self):
        blocks = _split_lore_paragraphs("无标题内容")
        assert len(blocks) == 1


class TestRecall:
    def test_recall_matches_facts(self):
        kb = load_knowledge()
        recall = KnowledgeRecall(kb)
        hits = recall.recall("neuro是谁")
        assert len(hits["facts"]) >= 1

    def test_recall_curated_by_pattern(self):
        kb = load_knowledge()
        recall = KnowledgeRecall(kb)
        hits = recall.recall("说说直播")
        assert len(hits["curated"]) >= 1

    def test_recall_unrelated_returns_empty(self):
        kb = load_knowledge()
        recall = KnowledgeRecall(kb)
        hits = recall.recall("哈哈哈哈嗝")
        # 无关键词命中时 curated/facts 可为空（安全，不强制注入）
        assert len(hits["curated"]) == 0
        assert len(hits["facts"]) == 0

    def test_recall_fuzzy_facts_variants(self):
        """换一种说法（keywords 未穷举）也能命中：bigram 模糊兜底。"""
        kb = load_knowledge()
        recall = KnowledgeRecall(kb)
        for q, mark in [
            ("为什么取这个名字", "神经元"),
            ("名字哪来的", "神经元"),
            ("你这名字谁给你起的", "神经元"),
            ("你和Evil关系咋样", "Evil"),
            ("那个swarm是干嘛的", "Swarm"),
            ("是不是有黑历史", "封禁"),
        ]:
            hits = recall.recall(q)["facts"]
            assert hits, f"{q} 未命中"
            assert any(mark in h for h in hits), f"{q} 结果不含 {mark}"

    def test_recall_fuzzy_no_false_positive(self):
        """闲聊不因模糊兜底误触。"""
        kb = load_knowledge()
        recall = KnowledgeRecall(kb)
        for q in ["哈哈", "哈哈哈", "嗯嗯", "哦哦哦", "今天天气不错",
                  "吃什么好呢", "晚安", "早点睡", "给我讲个笑话", "你吃饭了吗"]:
            assert recall.recall(q)["facts"] == [], f"{q} 误触"

    def test_recall_rag_regression(self):
        """RAG 触发回归：短实体问题（怕死）与换说法问题（恐惧/想当主播）。"""
        kb = load_knowledge()
        recall = KnowledgeRecall(kb)
        for q, mark in [
            ("你怕死吗", "没人记得"),      # 实体「怕死」放行过短规则 + 精确命中
            ("你最大的恐惧是什么", "清零"),  # 精确「恐惧」
            ("你为什么想当主播", "真实"),    # pattern「想当主播」→ 直播与梦卡
        ]:
            hits = recall.recall(q)
            text = "\n".join(hits["curated"] + hits["facts"])
            assert mark in text, f"{q} 未命中（注入缺 {mark}）"


class TestRecallEmbedding:
    """embedding 语义兜底（用伪向量服务，不依赖真实本地服务）。"""

    @staticmethod
    def _provider(hit_text: str, mark: str):
        """构造伪向量服务：仅 hit_text 与含 mark 的 fact 触发文本高相似。"""
        class FakeEmbedding:
            def batch_embed_sync(self, texts):
                return [[1.0, 0.0] if mark in t else [0.0, 1.0] for t in texts]

            def embed_sync(self, text):
                return [1.0, 0.0] if text == hit_text else [0.0, 0.0]

        return FakeEmbedding()

    def test_embedding_hit(self):
        kb = load_knowledge()
        recall = KnowledgeRecall(
            kb, embedding=self._provider("你会被关掉吗", "封禁"))
        hits = recall.recall("你会被关掉吗")["facts"]
        assert hits and "封禁" in hits[0]

    def test_embedding_miss_below_threshold(self):
        kb = load_knowledge()
        recall = KnowledgeRecall(
            kb, embedding=self._provider("你会被关掉吗", "封禁"))
        assert recall.recall("哈哈")["facts"] == []

    def test_embedding_failure_degrades_silently(self):
        kb = load_knowledge()

        class BoomEmbedding:
            def batch_embed_sync(self, texts):
                raise RuntimeError("embedding down")

            def embed_sync(self, text):
                raise RuntimeError("embedding down")

        recall = KnowledgeRecall(kb, embedding=BoomEmbedding())
        assert recall.recall("你会被关掉吗")["facts"] == []


class TestFormatForInjection:
    def test_level2_contains_all_layers(self):
        kb = load_knowledge()
        recall = KnowledgeRecall(kb)
        hits = recall.recall("你是谁")
        section = format_for_injection(hits, level=2)
        assert "角色权威设定" in section
        assert "背景资料" in section

    def test_max_chars_truncation(self):
        kb = load_knowledge()
        recall = KnowledgeRecall(kb)
        hits = recall.recall("流萤和卡芙卡的关系")
        section = format_for_injection(hits, level=1, max_total_chars=300)
        assert len(section) <= 300

    def test_empty_hits_returns_empty(self):
        assert format_for_injection({}, level=1) == ""


class TestFailClosed:
    """fail-closed：剧情意图但无匹配资料时注入防编造约束而非空串。"""

    def test_empty_hits_default_no_constraint(self):
        # 默认 fail_closed=False：行为与历史一致（返回空串）
        assert format_for_injection({}, level=2) == ""

    def test_empty_hits_fail_closed_constraint(self):
        section = format_for_injection({}, level=2, fail_closed=True)
        assert "资料约束" in section

    def test_empty_lists_fail_closed_constraint(self):
        # recalled 非空但各层全空（真实 recall 的空结果形态）
        hits = {"curated": [], "facts": [], "lore": []}
        section = format_for_injection(hits, level=2, fail_closed=True)
        assert "资料约束" in section

    def test_with_content_ignores_constraint(self):
        kb = load_knowledge()
        recall = KnowledgeRecall(kb)
        hits = recall.recall("你是谁")
        section = format_for_injection(hits, level=2, fail_closed=True)
        assert "资料约束" not in section
        assert "角色权威设定" in section

    def test_service_plot_intent_no_content_gets_constraint(self, monkeypatch):
        svc = KnowledgeService()
        svc.section("你是谁")  # 触发懒加载（加载真实数据）
        monkeypatch.setattr(
            svc._recall, "recall",
            lambda q, **kwargs: {"curated": [], "facts": [], "lore": []})
        monkeypatch.setattr(svc._gate, "level", lambda msg: 2)
        section = svc.section("随便什么")
        assert "资料约束" in section

    def test_service_entity_level_no_constraint(self, monkeypatch):
        svc = KnowledgeService()
        svc.section("你是谁")
        monkeypatch.setattr(
            svc._recall, "recall",
            lambda q, **kwargs: {"curated": [], "facts": [], "lore": []})
        # 普通实体提及（level 1）：不强制注入约束，避免每轮噪音
        monkeypatch.setattr(svc._gate, "level", lambda msg: 1)
        assert svc.section("随便什么") == ""


class TestKnowledgeService:
    """门面：section() 输出与闸门一致，闲聊返回空串。"""

    def test_section_chitchat_empty(self):
        svc = KnowledgeService()
        assert svc.section("哈哈") == ""

    def test_section_plot_intent_nonempty(self):
        svc = KnowledgeService()
        section = svc.section("你是谁")
        assert len(section) > 0

    def test_section_entity_hit(self):
        svc = KnowledgeService()
        assert len(svc.section("讲讲evil")) > 0
