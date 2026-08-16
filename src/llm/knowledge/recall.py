"""知识检索：curated 卡片 > facts 事实 > lore 段落（BM25 排序 + 语义兜底）。

facts 层在精确关键词之外提供两层兜底：
- 字符 bigram 相似度：换一种说法（如「名字的由来」而非「名字含义」）也能命中
- embedding 语义相似度（可选注入 provider）：完全不同的表述也能命中，
  如「你会被关掉吗」命中「死亡/重置」事实

lore 层在 BM25 排序之上提供两级语义兜底（level 1 严格门槛时启用）：
- 语义复核：BM25 中段命中（<10 分）用语义相似度剔除伪命中，
  「晚上吃什么好」误匹配「没什么好藏」的噪声段即由此剔除
- 语义补位：BM25 漏召回的答案段（如「列车组的第一站」的雅利洛段）
  用全库语义 topk 补回，numpy 矩阵乘一次算全量相似度

embedding 未配置或失败时静默降级，行为与未启用时完全一致。
无匹配内容时各层返回空列表，由 format 层决定是否实际注入。
"""

from __future__ import annotations

import threading

from src.llm.knowledge.bm25 import BM25
from src.llm.knowledge.loader import KnowledgeBase

# 模糊匹配阈值：query 与单个 keyword 的 bigram Dice 相似度达到此值即视为命中
_FUZZY_THRESHOLD = 0.15
# 精确命中的基础权重（远大于模糊分数，保证精确结果始终排前）
_EXACT_BONUS = 10.0
# embedding 语义兜底阈值：低于此值视为无关（实测 Qwen3-Embedding 本地模型
# 命中区 0.53+ / 闲聊区 0.34-0.43，取 0.50 兼顾命中与零误触）
_EMBED_THRESHOLD = 0.50
# lore BM25 命中门槛（中文 bigram 打分）：
# - level 1（普通实体提及）：7.0，实测相关剧情问句 7+、无关闲聊 <5，
#   严格门槛避免普通消息被注入背景资料
# - level 2（剧情意图，闸门已确认「你是谁/你来自哪里」等身份关系问题）：
#   2.5，放宽放行——这类问题正是 lore（角色亲历/世界观）的主战场，
#   实测「你是谁」相关段落 4.5 上下，过严会漏掉最典型的全层注入场景
_LORE_BM25_THRESHOLD = 7.0
_LORE_BM25_LOOSE_THRESHOLD = 2.5
# lore 语义复核/补位（level 1 下的两级语义兜底，阈值经真实数据标定）：
# - 语义复核：BM25 中段命中（<10 分）与查询做语义比对，相似度达标保留、
#   低于保留下限判伪命中剔除（「晚上吃什么好」9.06 分但语义仅 0.17）；
#   强命中（>=10 分）免复核，走零回归快速路径
# - 语义补位：BM25 命中不足时用全库语义 topk 补足槽位，达补位下限才补入
#   （「列车组的第一站」雅利洛段 BM25 6.45 漏掉、语义 0.43 补回）
# 实测分布：相关段 0.43+ / 噪声段 < 0.37，故复核保留 0.40、补位下限 0.42
_LORE_RECHECK_MAX = 10.0
_LORE_EMBED_KEEP = 0.40
_LORE_EMBED_MIN = 0.42
# lore 段落向量化截断长度：语义够用即可，短文本也天然降低批量嵌入的
# token 占用（服务端 n_ctx=2048，embed_batch_size=8 一次请求放得下）
_LORE_EMBED_CHARS = 200


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
        self._lore_bm25_cache: BM25 | None = None  # BM25 索引（docs 固定，缓存）
        self._lore_matrix = None  # 懒构建 (N,d) 归一化矩阵；[] = 不可用
        self._lore_matrix_lock = threading.Lock()
        self._lore_matrix_building = False

    def recall(self, query: str, *, top_k: int = 3, level: int = 1) -> dict:
        """返回 {"curated": [...], "facts": [...], "lore": [...]}。

        level：信号闸门判定的注入层级，lore 命中门槛随层级放宽
        （level>=2 剧情意图放行弱相关段落，level 1 仅强相关）。
        """
        return {
            "curated": self._match_curated(query),
            "facts": self._match_facts(query, top_k=2),
            "lore": self._match_lore(query, top_k=top_k, level=level),
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

    def _match_lore(self, query: str, top_k: int = 3, level: int = 1) -> list:
        """lore 段落：BM25 排序 + 语义复核/补位（level 1 时启用）。

        level>=2（剧情意图）：宽松门槛取 Top-K，语义已由闸门确认，
        不启用复核/补位（保持原行为）。level 1：严格门槛；中段命中
        （<10 分）用语义复核剔除伪命中，BM25 命中不足时语义补位。
        """
        if not self.kb.lore:
            return []
        threshold = (_LORE_BM25_LOOSE_THRESHOLD if level >= 2
                     else _LORE_BM25_THRESHOLD)
        scores = self._lore_bm25().score(query)
        ranked = sorted(enumerate(scores), key=lambda x: -x[1])
        hits = []
        if level >= 2:
            hits = [i for i, s in ranked[:top_k] if s >= threshold]
        else:
            pending = []
            for i, s in ranked:
                if s < threshold:
                    break
                if s >= _LORE_RECHECK_MAX:
                    hits.append(i)
                else:
                    pending.append(i)
            if len(hits) < top_k:
                hits.extend(self._recheck_lore(query, pending))
            hits = hits[:top_k]
            if len(hits) < top_k:
                hits.extend(self._fill_lore(query, top_k - len(hits), hits))
        return [self.kb.lore[i].content for i in hits[:top_k]]

    def _lore_bm25(self) -> BM25:
        """BM25 索引懒构建并缓存（docs 在进程内固定，避免每轮重建）。"""
        if self._lore_bm25_cache is None:
            self._lore_bm25_cache = BM25([b.content for b in self.kb.lore])
        return self._lore_bm25_cache

    def preheat_lore(self) -> None:
        """后台预构建 lore 语义矩阵（失败静默，不阻塞对话路径）。

        服务加载后即触发：首次对话需要语义补位时矩阵通常已就绪，
        避免在检索路径上同步构建（本地 118 段向量化耗时数秒）。
        """
        if self._embedding:
            threading.Thread(target=self._lore_embed_matrix, daemon=True).start()

    def _lore_embed_matrix(self):
        """lore 全量语义矩阵懒构建（失败冻结为空，行为与未启用一致）。

        并发保护：后台预热进行中再次触发时返回空（本次降级跳过），
        不阻塞对话路径等待向量化完成。
        """
        if self._lore_matrix is not None:
            return self._lore_matrix
        with self._lore_matrix_lock:
            if self._lore_matrix is not None:
                return self._lore_matrix
            if self._lore_matrix_building:
                return []
            if not self._embedding:
                self._lore_matrix = []
                return []
            self._lore_matrix_building = True
        try:
            import numpy as np

            texts = [b.content[:_LORE_EMBED_CHARS] for b in self.kb.lore]
            # 服务端 n_ctx=2048 + GPU：批量向量化一次请求，替代逐条串行
            vectors = self._embedding.batch_embed_sync(texts)
            if not vectors:
                raise RuntimeError("embedding 返回空结果")
            matrix = np.asarray(vectors, dtype=np.float32)
            norms = np.linalg.norm(matrix, axis=1, keepdims=True)
            norms[norms == 0] = 1.0  # 零向量行占位（相似度天然低于阈值）
            self._lore_matrix = matrix / norms
        except Exception:
            self._lore_matrix = []
        finally:
            self._lore_matrix_building = False
        return self._lore_matrix

    def _recheck_lore(self, query: str, candidates: list) -> list:
        """语义复核中段命中：相似度达标保留，否则判伪命中剔除。

        仅向量化候选段落（每轮至多中段命中数），不触发全量矩阵构建；
        embedding 不可用或失败时原样保留（行为与不启用语义一致）。
        """
        if not candidates or not self._embedding:
            return candidates
        try:
            import numpy as np

            q = np.asarray(self._embedding.embed_sync(query), dtype=np.float32)
            q_norm = float(np.linalg.norm(q))
            if q_norm == 0:
                return candidates
            q = q / q_norm
            texts = [self.kb.lore[i].content[:_LORE_EMBED_CHARS]
                     for i in candidates]
            vecs = self._embedding.batch_embed_sync(texts)
            kept = []
            for i, v in zip(candidates, vecs):
                v = np.asarray(v, dtype=np.float32)
                v_norm = float(np.linalg.norm(v))
                if v_norm and float(np.dot(v, q) / v_norm) >= _LORE_EMBED_KEEP:
                    kept.append(i)
            return kept
        except Exception:
            return candidates

    def _fill_lore(self, query: str, count: int, exclude: list) -> list:
        """向量补位：BM25 命中不足时用全库语义 topk 补足槽位。

        依赖预构建的 lore 矩阵（未就绪时返回空，本次降级跳过）；
        numpy 矩阵乘一次算出全库相似度，argpartition 取 top-k。
        """
        if count <= 0:
            return []
        matrix = self._lore_embed_matrix()
        if matrix is None or len(matrix) == 0:
            return []
        try:
            import numpy as np

            q = np.asarray(self._embedding.embed_sync(query), dtype=np.float32)
            q_norm = float(np.linalg.norm(q))
            if q.size == 0 or q_norm == 0:
                return []
            q = q / q_norm
            sims = matrix @ q
            exclude_set = set(exclude)
            top = np.argpartition(-sims, min(count, len(sims)) - 1)[:count]
            top = top[np.argsort(-sims[top])]
            return [int(i) for i in top
                    if int(i) not in exclude_set
                    and float(sims[i]) >= _LORE_EMBED_MIN]
        except Exception:
            return []
