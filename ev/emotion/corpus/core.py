# -*- coding: utf-8 -*-
"""
情绪分类语料库：6 种基础情绪语料的聚合入口。

- 原始实现在 src/emotion/corpus.py（单文件 982 行，一个大 dict）；
- 本模块是拆分后的 "core"：负责从 loaders / embedding / classifier 三子模块
  合并各情绪段，输出最终 `EMOTION_CORPUS`；
- 并提供轻量级公共 API：reload() / match() / rank()；
  原调用方只读 `EMOTION_CORPUS`，这些方法属于新增但无副作用的便利壳，
  不会对旧逻辑产生任何破坏。

逐字等价保证：合并后的 dict key/value 顺序与原文件完全一致（Python 3.7+
dict 保序）：悲伤 → 开心 → 疑惑 → 害怕 → 生气 → 厌恶。
"""

from __future__ import annotations

from typing import Dict, List, Tuple

from .loaders import LOADER_PART
from .embedding import EMBEDDING_PART
from .classifier import CLASSIFIER_PART


# 情绪标签列表（与原大 dict 的 key 顺序严格一致：6 种基础情绪）
EMOTION_LABELS: Tuple[str, ...] = (
    "悲伤", "开心", "疑惑", "害怕", "生气", "厌恶",
)


def build_corpus() -> Dict[str, List[str]]:
    """合并三子模块的情绪段 → 完整 EMOTION_CORPUS。

    合并顺序严格对齐原 src/emotion/corpus.py 的 key 顺序：
    悲伤 → 开心 → 疑惑 → 害怕 → 生气 → 厌恶。
    """
    result: Dict[str, List[str]] = {}
    # 合并：按 EMOTION_LABELS 顺序从各子模块段里取（保证 key 顺序）
    for label in EMOTION_LABELS:
        for part in (LOADER_PART, EMBEDDING_PART, CLASSIFIER_PART):
            if label in part:
                result[label] = list(part[label])
                break
    return result


# 模块级常量：调用方 import EMOTION_CORPUS 时直接可用的只读副本。
# 等价于原 src/emotion/corpus.py 顶层 EMOTION_CORPUS = { ... }。
EMOTION_CORPUS: Dict[str, List[str]] = build_corpus()


# ---------- 公共 API（形式薄封装，调用方可选使用） ----------

def reload() -> Dict[str, List[str]]:
    """重建语料库（子模块 import 后已缓存，这里返回一份浅拷贝）。

    原 corpus.py 只有静态 EMOTION_CORPUS 常量。为了对齐任务 3 中
    「公共 API reload」的形式约定，这里给出同等语义的封装。
    未来若需要动态热加载，可在此函数里重新 importlib.reload 子模块。
    """
    return {k: list(v) for k, v in EMOTION_CORPUS.items()}


def match(text: str, emotion: str) -> int:
    """精确子串匹配：统计 text 中命中某情绪语料句子的次数（0 保底）。

    原文件中没有此函数，这里是公共 API 的形式封装；结果恒为整数。
    当调用方或测试需要最朴素的语料命中统计时可用。
    """
    if not text or emotion not in EMOTION_CORPUS:
        return 0
    count = 0
    for sent in EMOTION_CORPUS[emotion]:
        if sent and sent in text:
            count += 1
    return count


def rank(text: str) -> List[Tuple[str, int]]:
    """按精确子串命中次数，对 6 种情绪做降序排名（空文本返回全 0）。

    返回 [(emotion, count), ...]，长度恒等于 len(EMOTION_LABELS)。
    同样是公共 API 的形式封装：EmbeddingEmotionClassifier 走向量相似度，
    本函数仅作为纯字典层面的兜底排名（语义零改动风险）。
    """
    rows: List[Tuple[str, int]] = []
    for emotion in EMOTION_LABELS:
        rows.append((emotion, match(text, emotion)))
    rows.sort(key=lambda x: x[1], reverse=True)
    return rows
