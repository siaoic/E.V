"""TTS Engine 的合成 + 播放方法（串行泵 / 流式 / 批量合成 / 字幕）。

全部以「接受 TTSEngine self 作为首参数」的模块级函数形式存在，
由 core.TTSEngine 的同名方法转发。
"""

from __future__ import annotations

import asyncio
import base64
import json
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


async def _synth_stream(
    self, text: str, gen: int, sfx: str = "", _retried: bool = False
) -> None:
    """Token 级流式合成一句（POST /tts/stream，SSE 边收边播）。"""
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
    payload = {
        "text": text,
        "speaker_audio": speaker_audio,
        "prompt_audio": prompt_audio,
        "prompt_text": prompt_text,
        "stream_chunk": _STREAM_CHUNK,
        "overlap_len": _STREAM_OVERLAP,
        **_SYNTH_PARAMS,
    }
    collected = bytearray()
    sent_id = None
    try:
        async with self._client.stream(
            "POST", f"{self._server_url}/tts/stream", json=payload
        ) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if gen != self._gen:
                    self._player.abort_stream()
                    return
                if not line.startswith("data: "):
                    continue
                evt = json.loads(line[6:])
                if evt.get("done"):
                    break
                pcm = base64.b64decode(evt["audio"])
                if sent_id is None:
                    sent_id = self._player.begin_stream(32000, text, gen)
                    if sent_id is None:
                        return
                    if sfx:
                        play_sfx_sequence([sfx])
                if evt.get("subtitles"):
                    self._player.feed_subtitles(evt["subtitles"], gen)
                collected += pcm
                if not self._player.feed_stream(bytes(pcm), gen):
                    return
        if sent_id is None:
            raise RuntimeError("流式无音频块")
        self._player.end_stream(gen)
        audio_data = (
            np.frombuffer(bytes(collected), dtype="<i2").astype(np.float32)
            / 32768.0
        )
        if audio_data.size:
            if _is_degraded_audio(
                len(audio_data) / 32000, len(text)
            ) or _has_burst_noise(audio_data):
                console.warn(
                    f"TTS 流式检测到异常合成（文本 {len(text)} 字、音频 "
                    f"{len(audio_data) / 32000:.1f}s），重试…"
                )
                if not _retried:
                    await self._synth_stream(text, gen, sfx, _retried=True)
            else:
                wav_bytes = await asyncio.to_thread(
                    _encode_wav_bytes, audio_data, 32000
                )
                if wav_bytes:
                    await asyncio.to_thread(
                        _cache_mod._cache_save, key, wav_bytes,
                        _cache_mod.evict_tts_cache,
                    )
    except Exception as e:
        self._player.abort_stream()
        if gen == self._gen:
            console.dim(f"TTS 流式合成失败（{e}）")
            if not _retried:
                await self._synth_stream(text, gen, sfx, _retried=True)


async def _synth_one(self, text: str, gen: int):
    """整句批量合成一句并下载解码，返回 (audio_data, sr)。

    仅供 provider.synthesize（一次性产出 wav 字节的对外交付接口）复用。
    """
    speaker_audio, prompt_audio, prompt_text = self._ref_params()
    payload = {
        "texts": [text],
        "speaker_audio": speaker_audio,
        "prompt_audio": prompt_audio,
        "prompt_text": prompt_text,
        **_SYNTH_PARAMS,
    }
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
        try:
            resp = await self._client.post(
                f"{self._server_url}/tts/batch", json=payload
            )
            resp.raise_for_status()
        except Exception as e:
            console.dim(f"TTS 合成失败（{e}），重试…")
            continue
        filenames = resp.json().get("filenames") or []
        if not filenames:
            return None
        for filename in filenames:
            audio = await self._download_audio(filename, key)
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
            break
    return None


async def _download_audio(
    self, filename: str, cache_key: str = ""
) -> Optional[Tuple[np.ndarray, int]]:
    """下载 wav 字节流并解码为 (1D float32, 采样率)。失败返回 None。"""
    try:
        resp = await self._client.get(f"{self._server_url}/audio/{filename}")
        resp.raise_for_status()
        content = resp.content
        if cache_key:
            await asyncio.to_thread(_cache_mod._cache_save, cache_key, content,
                                    _cache_mod.evict_tts_cache)
        data, sr = await asyncio.to_thread(_decode_wav_bytes, content)
        return np.asarray(data, dtype=np.float32).reshape(-1), int(sr)
    except Exception as e:
        console.dim(f"TTS 下载音频失败（{filename}）：{e}")
        return None


def _fallback_subtitles(text: str) -> list:
    """服务端不返回词级时间戳：构造整句时间戳整句显示。"""
    return [{"start_s": 0.0, "text": text, "orig_idx_end": len(text) - 1}]
