"""知识检索：curated 卡片 > facts 事实 > lore 段落（BM25 排序）。

facts 层在精确关键词之外提供两层兜底：
- 字符 bigram 相似度：换一种说法（如「名字的由来」而非「名字含义」）也能命中
- embedding 语义相似度（可选注入 provider）：完全不同的表述也能命中，
  如「你会被关掉吗」命中「死亡/重置」事实

embedding 未配置或失败时静默降级，行为与未启用时完全一致。
无匹配内容时各层返回空列表，由 format 层决定是否实际注入。
"""

from __future__ import annotations

from src.llm.knowledge.bm25 import BM25
from src.llm.knowledge.loader import KnowledgeBase

# 模糊匹配阈值：query 与单个 keyword 的 bigram Dice 相似度达到此值即视为命中
_FUZZY_THRESHOLD = 0.15
# 精确命中的基础权重（远大于模糊分数，保证精确结果始终排前）
_EXACT_BONUS = 10.0
# embedding 语义兜底阈值：低于此值视为无关（实测 Qwen3-Embedding 本地模型
# 命中区 0.53+ / 闲聊区 0.34-0.43，取 0.50 兼顾命中与零误触）
_EMBED_THRESHOLD = 0.50


def _bigrams(s: str) -> set:
    """字符二元组集合，用于中文短文本相似度匹配。"""
    s = s.lower()
    return {s[i:i + 2] for i in range(len(s) - 1)}


def _dice_sim(a: str, b: str) -> float:
    """两个字符串的字符 bigram Dice 相似度，范围 [0,1]。"""
    ba, bb = _bigrams(a), _bigrams(b)
    if not ba or not bb:
        return 0.0
    return 2.0 * len(ba & bb) / (len(ba) + len(bb))


def _cosine(a: list[float], b: list[float]) -> float:
    """余弦相似度（numpy 向量化，局部导入避免加重模块依赖）。"""
    import numpy as np

    va, vb = np.asarray(a, dtype=float), np.asarray(b, dtype=float)
    denom = float(np.linalg.norm(va) * np.linalg.norm(vb))
    if denom == 0:
        return 0.0
    return float(np.dot(va, vb) / denom)


class KnowledgeRecall:
    def __init__(self, kb: KnowledgeBase, embedding=None):
        """embedding：可选向量化服务（须提供 batch_embed_sync/embed_sync）。"""
        self.kb = kb
        self._embedding = embedding
        self._fact_vectors: list | None = None  # 懒构建 [(向量, answer)]

    def recall(self, query: str, *, top_k: int = 3) -> dict:
        """返回 {"curated": [...], "facts": [...], "lore": [...]}。"""
        return {
            "curated": self._match_curated(query),
            "facts": self._match_facts(query, top_k=2),
            "lore": self._match_lore(query, top_k=top_k),
        }

    def _match_curated(self, query: str) -> list:
        """curated 卡片：pattern 命中即返回全部（按 priority 降序）。"""
        hits = [(card.priority, card.content) for card in self.kb.curated
                if card.pattern.search(query)]
        hits.sort(key=lambda x: -x[0])
        return [content for _, content in hits]

    def _match_facts(self, query: str, top_k: int = 2) -> list:
        """facts 关键词精确匹配 + bigram 模糊 + embedding 语义兜底。

        有精确命中时只返回精确结果，不混入模糊命中——避免「你平时爱干什么」
        精确命中喜好事实后，又把含「干什么的」的自我介绍事实一起注入。
        """
        q = query.lower()
        bq = _bigrams(q)
        exact_hits, fuzzy_hits = [], []
        for fact in self.kb.facts:
            exact = sum(1 for kw in fact.keywords if kw.lower() in q)
            if exact:
                exact_hits.append((_EXACT_BONUS * fact.confidence, fact.answer))
                continue
            # 换说法兜底：与任一 keyword 相似度足够高即命中。
            # 交集 bigram 须 >=2，过滤「什么/怎么」这类高频词造成的单点噪声
            sim = 0.0
            for kw in fact.keywords:
                bkw = _bigrams(kw)
                inter = len(bq & bkw)
                if inter >= 2:
                    d = 2.0 * inter / (len(bq) + len(bkw))
                    if d > sim:
                        sim = d
            if sim >= _FUZZY_THRESHOLD:
                fuzzy_hits.append((sim * fact.confidence, fact.answer))
        if exact_hits:
            exact_hits.sort(key=lambda x: -x[0])
            return [answer for _, answer in exact_hits[:top_k]]
        if fuzzy_hits:
            fuzzy_hits.sort(key=lambda x: -x[0])
            return [answer for _, answer in fuzzy_hits[:top_k]]
        # 精确/bigram 都未命中时，才用 embedding 做语义兜底（最贵，最后用）
        return self._match_facts_embedding(query, top_k)

    def _ensure_fact_vectors(self) -> list:
        """懒构建全部 fact 的语义向量（keywords + answer 拼接）。"""
        if self._fact_vectors is not None or not self._embedding:
            return self._fact_vectors or []
        try:
            texts = [" ".join(f.keywords) + " " + f.answer for f in self.kb.facts]
            vectors = self._embedding.batch_embed_sync(texts)
            self._fact_vectors = [(v, f.answer) for v, f in zip(vectors, self.kb.facts) if v]
        except Exception:
            # embedding 不可用：冻结为空，静默降级（与未启用行为一致）
            self._fact_vectors = []
        return self._fact_vectors

    def _match_facts_embedding(self, query: str, top_k: int = 2) -> list:
        vectors = self._ensure_fact_vectors()
        if not vectors:
            return []
        try:
            qv = self._embedding.embed_sync(query)
        except Exception:
            return []
        if not qv:
            return []
        scored = sorted((_cosine(qv, v), ans) for v, ans in vectors)
        return [ans for s, ans in reversed(scored[-top_k:]) if s >= _EMBED_THRESHOLD]

    def _match_lore(self, query: str, top_k: int = 3) -> list:
        """lore 段落：BM25 打分取 Top-K（分数需为正，未命中跳过）。"""
        if not self.kb.lore:
            return []
        scores = BM25([b.content for b in self.kb.lore]).score(query)
        ranked = sorted(enumerate(scores), key=lambda x: -x[1])
        return [self.kb.lore[i].content
                for i, s in ranked[:top_k] if s > 0]
