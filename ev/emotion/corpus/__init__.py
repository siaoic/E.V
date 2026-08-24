"""情绪语料子包：聚合导出 EMOTION_CORPUS 主常量 + 公共 API。

旧路径 src/emotion/corpus.py 通过 `from ev.emotion.corpus import EMOTION_CORPUS`
做 forward，保证调用方 import 零改动。
"""

from .core import (  # noqa: F401
    EMOTION_CORPUS,
    EMOTION_LABELS,
    build_corpus,
    reload,
    match,
    rank,
)

__all__ = ["EMOTION_CORPUS", "EMOTION_LABELS", "build_corpus", "reload", "match", "rank"]
