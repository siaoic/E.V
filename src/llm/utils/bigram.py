"""轻量关键词召回：公共 2-gram 片段交集（无第三方分词依赖）。"""

import re


def _bigram_set(s: str) -> set:
    """文本的 2-gram 片段集合：中文按字符 2-gram 取交集（无第三方分词依赖）。"""
    s = re.sub(r"\s+", "", s)
    return {s[i:i + 2] for i in range(len(s) - 1)} if len(s) >= 2 else set()


def _bigram_hits(text: str, query: str) -> int:
    """公共 2-gram 片段数：轻量关键词召回（无第三方分词依赖）。

    中文按字符 2-gram 取交集，两个文本共享片段越多说明越相关。
    """
    return len(_bigram_set(text) & _bigram_set(query))
