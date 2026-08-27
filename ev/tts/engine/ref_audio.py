"""TTS 引擎的参考常量 + 文本清洗 + 合成退化检测 + wav 编解码 + 流式/合成参数。"""

from __future__ import annotations

import io
import re
import threading
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

# 流式合成参数（对齐 test_stream_20runs.py：stream_chunk=25, overlap_len=5）
# sovits_cache 随 stream_chunk 联动（见 _build_tts）：50 = stream_chunk*2，
# 55 = stream_chunk*2 + overlap_len。
_STREAM_CHUNK = 25
_STREAM_OVERLAP = 5

# 合成参数（对齐 test_basic_infer.py / test_stream_20runs.py 的 PARAMS，
# 即 gsv_tts 库默认采样档 top_k=15 / top_p=1.0）
_SYNTH_PARAMS = {
    "top_k": 15,
    "top_p": 1.0,
    "temperature": 1.0,
    "repetition_penalty": 1.35,
    "noise_scale": 0.5,
    "speed": 1.0,
}


# ---------- 本地 GSV-TTS 模型工厂（进程内合成，对齐 test_basic_infer / test_stream_20runs） ----------

# 预热短文本（启动时跑一句让模型权重就位，避免首句冷启动延迟）
_PREHEAT_TEXT = "你好。"

_tts_instance = None
_tts_build_lock = threading.Lock()
_tts_built = False


def _build_tts():
    """构建 GSV-TTS 本地单例（模型权重只加载一次，对齐 test_basic_infer / test_stream_20runs）。

    惰性加载 + 双重检查锁；TTS(use_bert=True, sovits_cache=[50, 55])，
    models_dir 用库默认（~/.cache/gsv 下的 s1v3.ckpt 与 s2Gv2ProPlus.pth），
    构造后显式 load_gpt_model / load_sovits_model 加载默认权重。推理为
    同步阻塞，需由调用方包到线程/线程池执行（core.start / playback 的 to_thread）。
    """
    global _tts_instance, _tts_built
    if _tts_built:
        return _tts_instance
    with _tts_build_lock:
        if _tts_built:
            return _tts_instance
        from gsv_tts import TTS

        _tts_instance = TTS(
            use_bert=True,
            sovits_cache=[_STREAM_CHUNK * 2, _STREAM_CHUNK * 2 + _STREAM_OVERLAP],
        )
        _tts_instance.load_gpt_model()
        _tts_instance.load_sovits_model()
        _tts_built = True
        return _tts_instance


def _preheat(tts, speaker_audio: str, prompt_audio: str, prompt_text: str) -> None:
    """预热推理路径：跑一句短文本走通「文本→BERT→GPT→SoVITS→解码」全链路。

    对齐 test_stream_20runs.py 预热语义——权重落入设备、kernel 编译完成，后续
    infer_stream 首块产出只受推理本身耗时限制。预热产物不播放；
    失败静默（首句多一次冷启动延迟，不阻断启动）。

    预热完成后把 GPT 的 prefill 切到 EFFICIENT backend：decode 的 CUDA Graph
    已在预热期用默认 CUDNN backend 捕获并固定（速度不变），而 prefill 走
    EFFICIENT 可避开 cuDNN 对每个新 token 长度触发 autotune 的首跑开销
    （约 400-500ms）。仅运行时修改模块级变量，不改任何库源码。
    """
    try:
        generator = tts.infer_stream(
            spk_audio_path=speaker_audio,
            prompt_audio_path=prompt_audio,
            prompt_audio_text=prompt_text,
            text=_PREHEAT_TEXT,
            stream_chunk=_STREAM_CHUNK,
            overlap_len=_STREAM_OVERLAP,
            return_subtitles=False,
            debug=False,
            **_SYNTH_PARAMS,
        )
        for _ in generator:
            pass
    except Exception:
        pass
    finally:
        # 切换 prefill backend（不改库源码）：t2s_model.SDPBACKEND 是模块级变量，
        # 构造/预热期以 CUDNN 捕获的 decode CUDA Graph 不受影响
        try:
            import gsv_tts.GPT_SoVITS.GPT.t2s_model as _tm
            from torch.nn.attention import SDPBackend as _SDPBackend

            _tm.SDPBACKEND = _SDPBackend.EFFICIENT_ATTENTION
        except Exception:
            pass
