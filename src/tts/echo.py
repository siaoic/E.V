"""TTS→STT 回声防护（3.14）：识别结果与最近播报文本相似则丢弃。

对标 hermes `tools/voice_mode.py` 的 is_tts_echo：打断场景（说话中用户插话）
存在被自己声音触发 STT 的潜在回环——扬声器漏音被麦克风拾取 → 识别出与
刚播报文本高度相似的内容 → 触发"自己回复自己"。用 difflib 字符级相似度
（跨语言无需分词）做 fail-closed 判定，命中即丢弃该识别结果。

E.V 原本通过"输出时丢弃输入"规避整段回环，但打断瞬间的漏音片段仍可能
穿过该机制，这里补上最后一道文本级防线（正常用户插话内容几乎不可能与
刚播报文本相似 ≥0.6，误杀风险极低）。
"""

import difflib
import re
import threading
from collections import deque
from typing import List

# 相似度阈值（difflib.SequenceMatcher 0..1）：≥0.6 判为回声
DEFAULT_TTS_ECHO_SIMILARITY_THRESHOLD = 0.6

# 窗口滑动兜底的最小识别文本长度：短于该长度不做片段匹配，避免把正常
# 单字插话（如 "对"）误判为回声（它可能恰好是长回复里的一个词）
MIN_FRAGMENT_LENGTH_FOR_ECHO = 10

# 最近播报文本窗口（句数）：覆盖"打断瞬间正在播 + 刚播完"的比对基准
_ECHO_WINDOW = 5

_lock = threading.Lock()
_recent_spoken: deque = deque(maxlen=_ECHO_WINDOW)


def remember_spoken(text: str) -> None:
    """TTS 播报文本时调用，记录为回声比对基准（进程内，无副作用）。"""
    text = (text or "").strip()
    if not text:
        return
    with _lock:
        _recent_spoken.append(text)


def recent_spoken_texts() -> List[str]:
    """返回最近播报文本列表（快照，从新到旧）。"""
    with _lock:
        return list(reversed(_recent_spoken))


def _normalize_for_echo_compare(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def is_tts_echo(
    transcript: str,
    spoken_text: str,
    threshold: float = DEFAULT_TTS_ECHO_SIMILARITY_THRESHOLD,
) -> bool:
    """判断 transcript 是否为 spoken_text 的自我捕获（回声）。

    先比全串相似度；未命中且 transcript 长度 ≥10 且短于 spoken_text 时，
    用等长滑动窗口在 spoken_text 上逐段比对（打断瞬间捕获的往往只是
    播报内容的一段碎片，全串比会因 spoken_text 过长而被稀释）。
    """
    if not transcript or not spoken_text:
        return False
    a = _normalize_for_echo_compare(transcript)
    b = _normalize_for_echo_compare(spoken_text)
    if not a or not b:
        return False
    if difflib.SequenceMatcher(None, a, b).ratio() >= threshold:
        return True
    if len(a) < MIN_FRAGMENT_LENGTH_FOR_ECHO or len(a) >= len(b):
        return False
    for start in range(0, len(b) - len(a) + 1):
        window = b[start : start + len(a)]
        if difflib.SequenceMatcher(None, a, window).ratio() >= threshold:
            return True
    return False


def is_echo_of_recent(
    transcript: str,
    threshold: float = DEFAULT_TTS_ECHO_SIMILARITY_THRESHOLD,
) -> bool:
    """判断识别文本是否与最近播报的任一文本构成回声。"""
    for spoken in recent_spoken_texts():
        if is_tts_echo(transcript, spoken, threshold):
            return True
    return False
