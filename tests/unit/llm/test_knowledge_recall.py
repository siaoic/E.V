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


class TestRecallRobustness:
    """检索鲁棒性：模糊换说法必须命中相关 lore，低相关度不得误注入。

    基于真实 data/knowledge 数据实证（2026-08-17）：
    - 模糊换说法 7 条全部命中，且 top-3（=实际注入内容）含答案标记
    - 低相关度 10 条在严格门槛（7.0）下全部不注入 lore
    - 三个已知盲区已按 C+D 方案修复（见 TestRecallEmbedding / facts 断言）：
      「晚上吃什么好」伪命中由语义复核剔除（C）、「列车组第一站」答案段
      由语义补位找回（C）、「停云是什么职位」由 facts 精确层作答（D）
    """

    def test_fuzzy_paraphrase_hits_top_lore(self):
        """换说法（keywords 未直接出现）也能命中，且 top-3 含答案段落。"""
        kb = load_knowledge()
        recall = KnowledgeRecall(kb)
        for q, mark in [
            ("开拓者醒来的时候在哪里", "黑塔空间站"),
            ("开拓者第一个到达的地方", "黑塔空间站"),
            ("罗浮仙舟是干嘛的", "仙舟"),
            ("演武大典上谁教三月七练剑", "演武大典"),
            ("流萤在匹诺康尼经历了什么", "匹诺康尼"),
            ("流萤和卡芙卡的关系", "卡芙卡"),
            ("姬子把开拓者带去了哪", "姬子"),
        ]:
            lore = recall.recall(q, level=1)["lore"]
            assert lore, f"{q} 未命中 lore"
            assert mark in "\n".join(lore), f"{q} 结果不含 {mark}"

    def test_low_relevance_no_lore_injection(self):
        """低相关度闲聊不注入任何 lore（严格门槛挡住）。"""
        kb = load_knowledge()
        recall = KnowledgeRecall(kb)
        for q in ["今天天气怎么样", "你吃饭了吗", "哈哈哈", "晚安",
                  "给我讲个笑话", "推荐一部电影吧", "你会唱歌吗",
                  "你觉得我可爱吗", "早上好呀", "中午吃什么"]:
            assert recall.recall(q, level=1)["lore"] == [], f"{q} 误注入"

    def test_entity_fact_regression(self):
        """D 方案：确定性实体事实下沉 facts 层精准作答（盲区 2 修复）。

        「停云/列车组第一站」这类实体→属性事实，语义检索也分不开噪声，
        由 facts 精确关键词层零成本命中。
        """
        kb = load_knowledge()
        recall = KnowledgeRecall(kb)
        for q, mark in [
            ("停云是什么职位", "接引使"),
            ("列车组的第一站", "雅利洛"),
        ]:
            hits = recall.recall(q, level=1)["facts"]
            assert hits and mark in "\n".join(hits), f"{q} facts 缺 {mark}"


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

    # ---- lore 语义复核 / 补位（C 方案，同样用伪向量服务离线验证） ----

    def test_lore_recheck_filters_false_positive(self):
        """C：中段 BM25 命中经语义复核剔除伪命中（盲区 1「晚上吃什么好」）。

        BM25 给 AI 自白段 9.06 分（误匹配「没什么好藏」），伪向量服务
        使查询与所有段落相似度都低于保留下限 → 中段命中被剔除、补位为空。
        """
        kb = load_knowledge()
        recall = KnowledgeRecall(
            kb, embedding=self._provider("晚上吃什么好", "不存在的标记"))
        assert recall.recall("晚上吃什么好", level=1)["lore"] == []

    def test_lore_fill_recovers_missed_answer(self):
        """C：BM25 漏召回的答案段经语义补位进注入（盲区 3「列车组第一站」）。

        答案段（雅利洛-VI 第一个开拓星球）BM25 仅 6.45 分漏掉；伪向量服务
        让答案段相似度最高 → 补位后进入 top-3 注入。
        """
        kb = load_knowledge()

        class FakeEmbedding:
            """查询与答案段（雅利洛第一站）语义最近，其他含「星穹列车」
            的背景段次之，无关段最低。"""

            def _vec(self, text):
                if "第一个开拓星球" in text:
                    return [1.0, 0.0]
                if "星穹列车" in text:
                    return [0.7, 0.3]
                return [0.0, 1.0]

            def batch_embed_sync(self, texts):
                return [self._vec(t) for t in texts]

            def embed_sync(self, text):
                return [1.0, 0.0] if text == "列车组的第一站" else self._vec(text)

        recall = KnowledgeRecall(kb, embedding=FakeEmbedding())
        lore = recall.recall("列车组的第一站", level=1)["lore"]
        assert any("雅利洛" in b for b in lore), lore

    def test_lore_recheck_keeps_strong_hits(self):
        """C：强命中（BM25>=10）免复核，语义路径全低分也保留（零回归）。"""
        kb = load_knowledge()
        recall = KnowledgeRecall(
            kb, embedding=self._provider("开拓者醒来的时候在哪里", "不存在的标记"))
        lore = recall.recall("开拓者醒来的时候在哪里", level=1)["lore"]
        assert any("黑塔空间站" in b for b in lore), lore

    def test_lore_embedding_failure_degrades(self):
        """embedding 故障：语义复核/补位静默降级，行为与未启用一致。"""
        kb = load_knowledge()

        class BoomEmbedding:
            def batch_embed_sync(self, texts):
                raise RuntimeError("embedding down")

            def embed_sync(self, text):
                raise RuntimeError("embedding down")

        recall = KnowledgeRecall(kb, embedding=BoomEmbedding())
        # 中段命中（9.06）无法复核 → 原样保留（与旧行为一致），不崩溃
        lore = recall.recall("晚上吃什么好", level=1)["lore"]
        assert lore, "降级后应保留 BM25 中段命中"


class TestFormatForInjection:
    def test_level2_contains_all_layers(self):
        kb = load_knowledge()
        recall = KnowledgeRecall(kb)
        # 与生产流程一致：闸门判 level 后带 level 召回，剧情意图放行 lore
        hits = recall.recall("你是谁", level=2)
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
        hits = recall.recall("你是谁", level=2)
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
