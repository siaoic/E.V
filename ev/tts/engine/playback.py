"""TTS Engine 的合成 + 播放方法（串行泵 / 流式 / 批量合成 / 字幕）。

全部以「接受 TTSEngine self 作为首参数」的模块级函数形式存在，
由 core.TTSEngine 的同名方法转发。

合成改为进程内直连 GSV-TTS-Lite（对齐 test_basic_infer / test_stream_20runs）：
_synth_local_stream 在 to_thread 线程内跑 tts.infer_stream 生成器，逐块喂给
播放器；退化检测、磁盘缓存、gen 代次打断等健壮性保留原样。
"""

from __future__ import annotations

import asyncio
from typing import Optional, Tuple

import numpy as np

from ev.utils import console
from ev.tts.echo import remember_spoken
from plugins.builtin.tools.sfx import play_sfx_sequence

from .ref_audio import (
    _HAS_CONTENT_RE,
    _collapse_tts_text,
    _decode_wav_bytes,
    _encode_wav_bytes,
    _is_degraded_audio,
    _has_burst_noise,
    _BATCH_MAX,
    _STREAM_CHUNK,
    _STREAM_OVERLAP,
    _SYNTH_PARAMS,
)
from . import cache as _cache_mod


# ---------- 对外 speak ----------

async def speak(self, text: str, sfx: str = "") -> None:
    """把一句话送入合成队列（立即返回，不阻塞 LLM 流）。"""
    if not self._ready or self._pending is None or self._interrupted:
        return
    text = (text or "").strip()
    if not text:
        if sfx:
            play_sfx_sequence([sfx])
        return
    if not _HAS_CONTENT_RE.search(text):
        return
    text = _collapse_tts_text(text)
    remember_spoken(text)
    await self._pending.put((text, sfx))
    if self._pump_task is None or self._pump_task.done():
        self._pump_task = asyncio.create_task(self._pump())


async def _pump(self) -> None:
    """串行消费待合成句子；收拢当前积攒的句子成批合成，空闲时退出。"""
    while True:
        item = await self._pending.get()
        if self._interrupted:
            continue
        gen = self._gen
        texts = [item]
        while len(texts) < _BATCH_MAX:
            try:
                texts.append(self._pending.get_nowait())
            except asyncio.QueueEmpty:
                break
        self._working = True
        try:
            await self._synth_remote(texts, gen)
        except asyncio.CancelledError:
            self._working = False
            raise
        except Exception as e:
            if gen == self._gen:
                console.error(f"TTS 合成失败：{e}")
        finally:
            self._working = False
        if self._pending.empty():
            return


async def _synth_remote(self, texts: list, gen: int) -> None:
    """逐句合成并播放（只走 Token 级流式）。"""
    for text, sfx in texts:
        if gen != self._gen:
            return
        await self._synth_stream(text, gen, sfx)


# ---------- 流式合成（进程内 infer_stream 生成器）----------

def _synth_local_stream(
    self, text: str, gen: int, sfx: str,
    speaker_audio: str, prompt_audio: str, prompt_text: str, key: str,
) -> bool:
    """同步：进程内跑 infer_stream 生成器，逐块喂给播放器（to_thread 线程内）。

    对齐 test_stream_20runs.py 流式消费：首块到达即开播（begin_stream），
    逐块 feed_subtitles / feed_stream；收集整段用于退化检测与磁盘缓存写回。
    返回 True 表示检测到退化合成（调用方应重试一次）；异常向上抛。
    """
    tts = self._tts
    if tts is None:
        return False
    generator = tts.infer_stream(
        spk_audio_path=speaker_audio,
        prompt_audio_path=prompt_audio,
        prompt_audio_text=prompt_text,
        text=text,
        stream_chunk=_STREAM_CHUNK,
        overlap_len=_STREAM_OVERLAP,
        return_subtitles=True,
        debug=False,
        **_SYNTH_PARAMS,
    )
    collected = []
    sent_id = None
    sr = 32000
    try:
        for chunk in generator:
            if gen != self._gen:
                self._player.abort_stream()
                return False
            audio_data = np.asarray(
                chunk.audio_data, dtype=np.float32).reshape(-1)
            sr = int(getattr(chunk, "samplerate", sr) or sr)
            if sent_id is None:
                sent_id = self._player.begin_stream(sr, text, gen)
                if sent_id is None:
                    return False
                if sfx:
                    play_sfx_sequence([sfx])
            subtitles = getattr(chunk, "subtitles", None)
            if subtitles:
                self._player.feed_subtitles(subtitles, gen)
            collected.append(audio_data)
            pcm = (audio_data * 32768.0).astype("<i2").tobytes()
            if not self._player.feed_stream(pcm, gen):
                return False
    except Exception:
        self._player.abort_stream()
        raise
    if sent_id is None:
        raise RuntimeError("流式无音频块")
    self._player.end_stream(gen)
    audio_data = np.concatenate(collected)
    if _is_degraded_audio(len(audio_data) / sr, len(text)) or _has_burst_noise(
        audio_data
    ):
        console.warn(
            f"TTS 流式检测到异常合成（文本 {len(text)} 字、音频 "
            f"{len(audio_data) / sr:.1f}s），重试…"
        )
        _cache_mod._cache_delete(key)
        return True
    wav_bytes = _encode_wav_bytes(audio_data, sr)
    if wav_bytes:
        _cache_mod._cache_save(key, wav_bytes, _cache_mod.evict_tts_cache)
    return False


async def _synth_stream(
    self, text: str, gen: int, sfx: str = "", _retried: bool = False
) -> None:
    """Token 级流式合成一句（进程内 infer_stream，边合成边播）。"""
    speaker_audio, prompt_audio, prompt_text = self._ref_params()
    key = _cache_mod._tts_cache_key(
        text, speaker_audio, prompt_audio, prompt_text, _SYNTH_PARAMS)
    cached = await asyncio.to_thread(_cache_mod._cache_load, key)
    if cached is not None:
        if gen != self._gen:
            return
        audio = await asyncio.to_thread(_decode_wav_bytes, cached)
        audio_data, sr = audio
        if _is_degraded_audio(len(audio_data) / sr, len(text)) or _has_burst_noise(
            audio_data
        ):
            await asyncio.to_thread(_cache_mod._cache_delete, key)
        else:
            if sfx:
                play_sfx_sequence([sfx])
            self._player.emit(audio_data, sr, text, _fallback_subtitles(text), gen)
            return
    try:
        degraded = await asyncio.to_thread(
            _synth_local_stream, self, text, gen, sfx,
            speaker_audio, prompt_audio, prompt_text, key,
        )
    except Exception as e:
        if gen == self._gen and not _retried:
            console.dim(f"TTS 流式合成失败（{e}）")
            await self._synth_stream(text, gen, sfx, _retried=True)
        return
    if degraded and not _retried:
        await self._synth_stream(text, gen, sfx, _retried=True)


# ---------- 整句合成（provider.synthesize 复用）----------

def _synth_local_one(
    self, text: str, gen: int,
    speaker_audio: str, prompt_audio: str, prompt_text: str, key: str,
) -> Optional[Tuple[np.ndarray, int]]:
    """同步：进程内整句合成（infer 整句），含退化检测与缓存写回。

    返回 (audio_data, sr)；退化或失败时删除缓存并返回 None。
    """
    tts = self._tts
    if tts is None:
        return None
    try:
        clip = tts.infer(
            spk_audio_path=speaker_audio,
            prompt_audio_path=prompt_audio,
            prompt_audio_text=prompt_text,
            text=text,
            **_SYNTH_PARAMS,
        )
        audio_data = np.asarray(clip.audio_data, dtype=np.float32).reshape(-1)
        sr = int(getattr(clip, "samplerate", 32000) or 32000)
        if _is_degraded_audio(len(audio_data) / sr, len(text)) or _has_burst_noise(
            audio_data
        ):
            _cache_mod._cache_delete(key)
            return None
        wav_bytes = _encode_wav_bytes(audio_data, sr)
        if wav_bytes:
            _cache_mod._cache_save(key, wav_bytes, _cache_mod.evict_tts_cache)
        return audio_data, sr
    except Exception as e:
        console.dim(f"TTS 合成失败（{e}）")
        return None


async def _synth_one(self, text: str, gen: int):
    """整句批量合成一句并解码，返回 (audio_data, sr)。

    仅供 provider.synthesize（一次性产出 wav 字节的对外交付接口）复用。
    """
    speaker_audio, prompt_audio, prompt_text = self._ref_params()
    key = _cache_mod._tts_cache_key(
        text, speaker_audio, prompt_audio, prompt_text, _SYNTH_PARAMS)
    cached = await asyncio.to_thread(_cache_mod._cache_load, key)
    if cached is not None:
        audio = await asyncio.to_thread(_decode_wav_bytes, cached)
        audio_data, sr = audio
        if not (
            _is_degraded_audio(len(audio_data) / sr, len(text))
            or _has_burst_noise(audio_data)
        ):
            return audio_data, sr
        await asyncio.to_thread(_cache_mod._cache_delete, key)
    for _ in range(2):
        if gen != self._gen:
            return None
        audio = await asyncio.to_thread(
            _synth_local_one, self, text, gen,
            speaker_audio, prompt_audio, prompt_text, key)
        if audio is None:
            continue
        audio_data, sr = audio
        if not (
            _is_degraded_audio(len(audio_data) / sr, len(text))
            or _has_burst_noise(audio_data)
        ):
            return audio_data, sr
        await asyncio.to_thread(_cache_mod._cache_delete, key)
        console.warn(
            f"TTS 检测到异常合成（文本 {len(text)} 字、音频 "
            f"{len(audio_data) / sr:.1f}s），重试…"
        )
    return None


def _fallback_subtitles(text: str) -> list:
    """无词级时间戳时：构造整句时间戳整句显示。"""
    return [{"start_s": 0.0, "text": text, "orig_idx_end": len(text) - 1}]
