"""TTS 回声防护（3.14）：识别结果与最近播报文本比对。

STT 在「AI 开口瞬间被自己声音打断」的回环场景下，麦克风会把扬声器漏音
识别成用户语音。本模块维护一个最近播报文本的环形缓冲，供
``ev/asr/stt.py`` 在投递识别结果前做相似度判决：

- 相似度 ≥ 0.6（difflib SequenceMatcher）→ 判为回声，丢弃；
- 否则正常投递。

只做内存环形缓冲，不持久化；缓冲容量与文本量都很小，不引入额外依赖。
"""
from __future__ import annotations

import difflib
import threading
from collections import deque
from typing import Optional

# 最近播报文本缓冲上限 / 相似度阈值（对齐 config AGENT_TTS_ECHO_GUARD 注释）
_MAX_SAMPLES = 8
_MAX_CHARS = 400
_SIM_THRESHOLD = 0.6

_lock = threading.Lock()
_recent: deque = deque(maxlen=_MAX_SAMPLES)


def record_spoken(text: str) -> None:
    """记录一段新播报文本（TTS 引擎在 speak 时调用）。"""
    text = (text or "").strip()
    if not text:
        return
    with _lock:
        _recent.append(text[: _MAX_CHARS])


def is_echo_of_recent(text: str, threshold: float = _SIM_THRESHOLD) -> bool:
    """判断识别文本是否与最近播报文本高度相似（扬声器回声）。

    空缓冲 / 空文本恒返回 False。相似度取与所有缓冲样本的最大值。
    """
    text = (text or "").strip()
    if not text:
        return False
    with _lock:
        samples = list(_recent)
    if not samples:
        return False
    best = 0.0
    for s in samples:
        sim = difflib.SequenceMatcher(None, s, text).ratio()
        if sim > best:
            best = sim
        if best >= threshold:
            return True
    return best >= threshold


__all__ = ["record_spoken", "is_echo_of_recent"]
