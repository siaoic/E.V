"""流式音频播放器（AsyncAudioPlayer）。

对齐 GSV-TTS-Lite 的 AudioQueue（threading + sounddevice.OutputStream）
原生播放语义：每个 AudioClip 通过其自带 ``play()`` 写入 GSV 的 AudioQueue，
由库内部播放线程顺序出声。本模块在上层补一层「段/块」编排：

- ``begin_stream(sr, text, gen)``：一句话开始流式合成时登记，分配 ``sent_id``。
- ``emit(audio, sr, text, subtitles, gen)``：每个 AudioClip 产出时上报
  （bench 脚本在首块到达时刻打钩子）。
- ``_emit_block(chunk, sr, sent_id, chunk_idx, gen)``：真正把一块音频交给
  声卡前调用（``chunk.play()`` 写入 GSV AudioQueue），是「LLM 首字→TTS 首块」
  延迟测量的核心埋点；首块（chunk_idx==0）同时触发 ``_on_play`` 回调
  （口型 / 字幕同步）。

与 GSV 库的 AudioQueue 分工：本播放器不复制声卡逻辑，只做编排与回调；
实际出声仍由 ``tts.audio_queue``（GSV 原生）驱动，避免重复实现。

线程模型：合成线程（engine 的 pump worker）调 ``_emit_block`` 把 AudioClip
丢进 GSV 播放队列后立即返回；出声进度由库内播放线程推进，``_on_play`` 在
合成线程回调（同步安全，不阻塞）。
"""
from __future__ import annotations

import threading
from typing import Callable, Optional


class AsyncAudioPlayer:
    """流式播放编排器。

    每句话一个 ``gen``（infer_stream 生成器）；句子开始合成时
    ``begin_stream`` 登记，之后每个 AudioClip 产出经 ``emit`` 上报、
    ``_emit_block`` 实际播放。首块出声触发 ``_on_play``（口型 / 字幕）。
    """

    def __init__(self) -> None:
        self._sent_id = 0
        self._gen_to_sid: dict = {}      # id(gen) -> (sid, gen, sr)
        self._first_fired: set = set()   # 已触发过出声回调的 sent_id
        self._on_play: Optional[Callable] = None
        self._lock = threading.Lock()

    # --------------------------- 段登记 / 编排 ---------------------------

    def begin_stream(self, sr: int, text: str, gen) -> int:
        """登记一句新流：分配 sent_id，记录其生成器与参考采样率。

        返回该句的 sent_id，后续该句所有 AudioClip 都挂到它下面，
        便于 bench 脚本按句聚合首块时刻。
        """
        with self._lock:
            self._sent_id += 1
            sid = self._sent_id
            self._gen_to_sid[id(gen)] = (sid, gen, sr)
        return sid

    def emit(self, audio, sr: int, text: str, subtitles, gen) -> int:
        """上报一块合成好的 AudioClip（bench 钩子点）。

        首次出现某 gen 时自动登记 sent_id。返回该块所属 sent_id
        （0 = 未登记成功）。
        """
        with self._lock:
            entry = self._gen_to_sid.get(id(gen))
            if entry is None and gen is not None:
                self._sent_id += 1
                entry = (self._sent_id, gen, sr)
                self._gen_to_sid[id(gen)] = entry
            sid = entry[0] if entry else 0
        return sid

    def _emit_block(self, chunk, sr: int, sent_id: int, chunk_idx: int, gen):
        """一块音频交给声卡前的钩子：实际写入 GSV AudioQueue 播放。

        bench_llm_first_byte.py monkey-patch 此方法记录「TTS 首块入播放
        队列」时刻。首块（chunk_idx==0）播放时触发 ``_on_play`` 回调
        （口型 / 字幕）。
        """
        if chunk is None:
            return
        try:
            chunk.play()
        except Exception:
            pass
        if chunk_idx == 0 and sent_id not in self._first_fired:
            self._first_fired.add(sent_id)
            dur_s = float(getattr(chunk, "audio_len_s", 0.5) or 0.5)
            text = getattr(chunk, "orig_text", "") or ""
            cb = self._on_play
            if cb is not None:
                try:
                    cb(None, text, dur_s)
                except Exception:
                    pass

    # --------------------------- 回调配置 ---------------------------

    def set_on_play_callback(self, cb: Optional[Callable]) -> None:
        """设置出声回调（on_play(wav, text, dur_s)）。"""
        self._on_play = cb


__all__ = ["AsyncAudioPlayer"]
