"""TTS 引擎的参考常量 + 文本清洗 + 合成退化检测 + wav 编解码 + 流式/合成参数。"""

from __future__ import annotations

import io
import re
from typing import Tuple

import numpy as np
import soundfile as sf

# 多参考音频分隔符
_REF_AUDIO_SEP = "|"

# 实质内容字符集合：CJK/日/韩/字母数字
_HAS_CONTENT_RE = re.compile(r"[\w\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]")

# 防合成退化的文本清洗正则
_URL_RE = re.compile(r"https?://[^\s，。！？、；：]+", re.IGNORECASE)
_KAOMOJI_RE = re.compile(r"[（(][^（）()0-9A-Za-z\u4e00-\u9fff]*[)）]")
_REPEAT_PUNCT_RE = re.compile(r"([！？。，、；：～~])\1+")
_REPEAT_ALNUM_RE = re.compile(r"(\w)\1{3,}")
_REPEAT_SYLLABLE_RE = re.compile(r"([A-Za-z]{1,3})\1{2,}")

# 合成退化兜底检测：时长异常（拖长音怪叫）
_MAX_DEGRADED_SEC = 25.0
_MAX_SEC_PER_CHAR = 1.0
_MIN_DEGRADED_SEC = 8.0

# 合成退化兜底检测之二：尖峰噪声（交替满幅振荡）
_BURST_DIFF_THRESH = 0.5
_BURST_RATE_MAX = 0.01


def _is_degraded_audio(dur_s: float, text_len: int) -> bool:
    """判断合成音频是否退化（拖长音怪叫）：时长异常地远超文本长度。"""
    return dur_s > _MAX_DEGRADED_SEC or (
        dur_s > _MIN_DEGRADED_SEC and dur_s / max(1, text_len) > _MAX_SEC_PER_CHAR
    )


def _has_burst_noise(audio: np.ndarray) -> bool:
    """判断音频是否含交替满幅振荡崩坏段（高频嘶声）。"""
    if audio.size < 100:
        return False
    d = np.abs(np.diff(audio))
    return float((d > _BURST_DIFF_THRESH).mean()) > _BURST_RATE_MAX


def _collapse_tts_text(text: str) -> str:
    """合成前压缩易致怪叫的文本：去 URL/颜文字、折叠重复标点/语气字/音节。"""
    if not text:
        return text
    text = _URL_RE.sub(" ", text)
    text = _KAOMOJI_RE.sub("", text)
    text = _REPEAT_PUNCT_RE.sub(r"\1", text)
    text = _REPEAT_SYLLABLE_RE.sub(r"\1\1", text)
    text = _REPEAT_ALNUM_RE.sub(r"\1\1", text)
    return text


def _decode_wav_bytes(content: bytes) -> Tuple[np.ndarray, int]:
    """线程池执行的 wav 解码：从字节流还原 (1D ndarray, 采样率)。"""
    data, sr = sf.read(io.BytesIO(content), dtype="float32")
    return data, int(sr)


def _encode_wav_bytes(audio: np.ndarray, sr: int) -> bytes:
    """把 1D float32 音频编码为 wav 字节流（流式整句写磁盘缓存用）。"""
    buf = io.BytesIO()
    sf.write(buf, audio, sr, format="WAV")
    return buf.getvalue()


# 服务端默认地址（fastapi_server_example.py 监听 0.0.0.0:8000）
_SERVER_DEFAULT_URL = "http://127.0.0.1:8000"

# 泵单次收拢句数：固定 1（每句独立合成，一句失败只影响自身）
_BATCH_MAX = 1

# 流式合成参数
_STREAM_CHUNK = 25
_STREAM_OVERLAP = 5

# 合成参数（服务端 /tts/stream 透传到 infer_stream）
_SYNTH_PARAMS = {
    "top_k": 5,
    "top_p": 0.9,
    "temperature": 1.0,
    "repetition_penalty": 1.35,
    "noise_scale": 0.5,
    "speed": 1.0,
}
