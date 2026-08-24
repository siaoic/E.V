"""轻量 BM25 检索（零第三方依赖，仅用于 lore 段落排序）。

关键词拆分为正则提取的中文片段 / 英文单词，用经典 BM25 公式打分。
语料为知识库 lore 段落（几十条量级），每次检索重建索引可忽略开销。
"""

from __future__ import annotations

import math
import re

_TOKEN_RE = re.compile(r"[a-zA-Z0-9_]+|[\u4e00-\u9fff]+")


def _tokenize(text: str) -> list:
    """粗粒度分词：中文按滑动双字（bigram），英文按单词。

    单字 BM25 对短中文查询过宽（实测「今天天气怎么样」命中 74/118 段落，
    普通消息被误注入背景资料）；bigram 显著提升相关/无关分离度
    （实测 9/118），配合 recall 的分数门槛，保证只有真正相关的剧情问句
    才会带上知识库背景。
    """
    tokens = []
    for m in _TOKEN_RE.findall(text.lower()):
        if m[0].isascii():
            tokens.append(m)
        else:
            tokens.extend(m[i:i + 2] for i in range(len(m) - 1))
    return tokens


class BM25:
    """经典 BM25（k1=1.5, b=0.75），docs 为原始文本列表。"""

    def __init__(self, docs: list, *, k1: float = 1.5, b: float = 0.75):
        self._docs = docs
        self._corpus_tokens = [list(_tokenize(d)) for d in docs]
        self._avgdl = (
            sum(len(t) for t in self._corpus_tokens) / len(self._corpus_tokens)
            if self._corpus_tokens else 1.0
        )
        self._idf: dict = {}
        for tokens in self._corpus_tokens:
            for token in set(tokens):
                self._idf[token] = self._idf.get(token, 0) + 1
        n_docs = max(len(self._corpus_tokens), 1)
        for token, df in self._idf.items():
            self._idf[token] = math.log(1 + (n_docs - df + 0.5) / (df + 0.5))
        self._k1 = k1
        self._b = b

    def score(self, query: str) -> list:
        """返回与 docs 等长的分数列表（未命中的 doc 为 0）。"""
        q_tokens = set(_tokenize(query))
        if not q_tokens or not self._corpus_tokens:
            return [0.0] * len(self._docs)
        scores = []
        for tokens in self._corpus_tokens:
            if not tokens:
                scores.append(0.0)
                continue
            dl = len(tokens)
            freq = {}
            for token in tokens:
                if token in q_tokens:
                    freq[token] = freq.get(token, 0) + 1
            if not freq:
                scores.append(0.0)
                continue
            norm = 1 - self._b + self._b * dl / self._avgdl
            scores.append(sum(
                self._idf.get(token, 0.0) * f * (self._k1 + 1) / (f + self._k1 * norm)
                for token, f in freq.items()))
        return scores
