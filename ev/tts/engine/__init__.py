"""ev.tts.engine：TTSEngine 进程内流式合成引擎包。

兼容两种导入：
- ``from ev.tts.engine import TTSEngine``
- ``from ev.tts.engine.core import TTSEngine``
"""
from .core import (
    TTSEngine,
    _STREAM_CHUNK,
    _STREAM_OVERLAP,
    _SYNTH_PARAMS,
    _wav_cache,
    _cleanup_output,
    evict_tts_cache,
)

__all__ = [
    "TTSEngine",
    "_STREAM_CHUNK",
    "_STREAM_OVERLAP",
    "_SYNTH_PARAMS",
    "_wav_cache",
    "_cleanup_output",
    "evict_tts_cache",
]