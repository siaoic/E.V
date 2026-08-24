"""TTS engine 子包：导出 TTSEngine 类 + 模块级对外符号。"""

from .core import TTSEngine
from .cache import (  # noqa: F401
    evict_tts_cache, _wav_cache, _cleanup_output,
    _tts_cache_dir, _tts_cache_key,
    _cache_save, _cache_load, _cache_delete,
    _TTS_CACHE_MAX_BYTES, _TTS_CACHE_TTL_SEC,
)
from .ref_audio import _REF_AUDIO_SEP, _SYNTH_PARAMS, _SERVER_DEFAULT_URL
from .playback import _fallback_subtitles

__all__ = ["TTSEngine", "evict_tts_cache"]
