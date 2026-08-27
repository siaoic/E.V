"""TTSEngine 类骨架：__init__ + 生命周期 + 对外接口。

播放/合成实现在 playback.py，磁盘缓存在 cache.py，常量/文本清洗/本地
模型工厂在 ref_audio.py。合成改为进程内直连 GSV-TTS-Lite
（对齐 test_basic_infer / test_stream_20runs），不再依赖外部 HTTP 服务
（ev/tts/server.py 仅保留供调试用）。
"""

from __future__ import annotations

import asyncio
from typing import Optional, Tuple

from ev.utils import console
from ev.tts.player import TTSPlayer
from ev.adapter.tts import BaseTTSAdapter

from .ref_audio import (
    _build_tts,
    _preheat,
)
from . import cache as _cache_mod
from . import playback as _playback_mod


class TTSEngine(BaseTTSAdapter):
    """GPT-SoVITS 本地引擎（进程内直连 GSV-TTS-Lite，合成与播放同进程）。

    对齐 test_basic_infer / test_stream_20runs：TTS 单例 + infer_stream 流式
    合成；播放层沿用 TTSPlayer（口型/字幕/打断强依赖）。
    """

    def __init__(self) -> None:
        from ev.utils import config as _config

        self._ref_main = str(
            getattr(_config.cfg, "GPTSOVITS_REF_AUDIO", "") or ""
        ).strip()
        self._ref_extras = str(
            getattr(_config.cfg, "GPTSOVITS_REF_AUDIOS", "") or ""
        ).strip()
        self.ref_text = str(
            getattr(_config.cfg, "GPTSOVITS_PROMPT_TEXT", "") or ""
        ).strip()

        self._tts = None        # 本地 GSV-TTS 实例（start() 加载后非空）
        self._ready = False
        self._player = TTSPlayer()
        self._gen = 0
        self._interrupted = False
        self._pending: Optional[asyncio.Queue] = None
        self._pump_task: Optional[asyncio.Task] = None
        self._working = False

    # ---------- 参考音频 ----------

    def _ref_params(self) -> Tuple[str, str, str]:
        """参考参数：(speaker_audio, prompt_audio, prompt_text)。"""
        main = (self._ref_main or "").strip()
        return main, main, (self.ref_text or "").strip()

    # ---------- 生命周期 ----------

    async def start(self) -> bool:
        """加载本地 GSV-TTS 模型（未配置参考音频或加载失败时返回 False）。"""
        if not self._ref_params()[0]:
            console.warn("TTS：未配置 GPTSOVITS_REF_AUDIO，语音合成关闭")
            return False
        self._player.set_loop(asyncio.get_running_loop())
        try:
            # 模型加载约 20s：to_thread 避免阻塞事件循环
            # （init_tts_async 是后台任务，期间 speak/drain 由守卫静默处理）
            self._tts = await asyncio.to_thread(_build_tts)
        except Exception as e:
            console.warn(f"TTS：本地模型加载失败（{e}），语音将降级为纯字幕")
            self._tts = None
            return False
        self._pending = asyncio.Queue()
        self._ready = True
        console.ok("TTS 本地模型已加载（进程内合成）")
        asyncio.create_task(asyncio.to_thread(_cache_mod.evict_tts_cache))
        try:
            import numpy as _np

            sr_warm = 32000
            self._player._ensure_queue(sr_warm)
            warmup = _np.zeros(int(0.05 * sr_warm), dtype=_np.float32)
            self._player._queue.put(warmup.reshape(-1, 1))
        except Exception:
            pass
        return True

    async def warmup(self) -> None:
        """预热：跑一句短文本走通完整推理链路（对齐 test_stream_20runs.py）。"""
        if not self._ready or self._tts is None:
            return
        speaker_audio, prompt_audio, prompt_text = self._ref_params()
        if not speaker_audio:
            return
        try:
            await asyncio.to_thread(
                _preheat, self._tts, speaker_audio, prompt_audio, prompt_text)
            console.dim("[TTS] 本地预热完成（推理链路已就绪）")
        except Exception as e:
            console.dim(f"[TTS] 预热失败（不影响使用）：{e}")

    def preheat(self) -> None:
        """每轮对话起始预热：仅本地播放链路。"""
        if not self._ready or self._tts is None or self._interrupted:
            return
        try:
            import numpy as _np

            self._player._ensure_queue(32000)
            warmup = _np.zeros(int(0.05 * 32000), dtype=_np.float32)
            self._player._queue.put(warmup.reshape(-1, 1))
        except Exception:
            pass

    async def stop(self) -> None:
        """停止：打断播放/合成并释放本地模型引用。"""
        self.interrupt()
        if self._pump_task is not None and not self._pump_task.done():
            try:
                await asyncio.wait_for(self._pump_task, timeout=5)
            except Exception:
                try:
                    self._pump_task.cancel()
                except Exception:
                    pass
        self._pump_task = None
        self._player.close()
        self._tts = None
        self._ready = False

    async def drain(self) -> None:
        """等待全部已提交句子合成完成 + 全部音频播完。"""
        while self._working or (
            self._pending is not None and not self._pending.empty()
        ):
            if (
                self._pending is not None
                and not self._pending.empty()
                and (self._pump_task is None or self._pump_task.done())
            ):
                self._pump_task = asyncio.create_task(self._pump())
            await asyncio.sleep(0.05)
        if self._pump_task is not None and not self._pump_task.done():
            try:
                await asyncio.wait_for(self._pump_task, timeout=300)
            except Exception:
                pass
        await self._player.drain()

    # ---------- 对外接口（main.py / stream.py 调用）----------

    def set_on_play_callback(self, cb: Optional[object]) -> None:
        self._player.set_on_play_callback(cb)

    def set_on_play_done_callback(self, cb: Optional[object]) -> None:
        self._player.set_on_play_done_callback(cb)

    def set_subtitle_callback(self, cb: Optional[object]) -> None:
        self._player.set_subtitle_callback(cb)

    def apply_ref(self, audio: str, text: str) -> None:
        """热更新参考音频/文本（仅更新主参考）。"""
        self._ref_main = (audio or "").strip()
        self.ref_text = (text or "").strip()

    def apply_ref_extras(self, extras: str) -> None:
        """热更新辅助参考音频（多条 | 分隔）。"""
        self._ref_extras = (extras or "").strip()

    def interrupt(self) -> None:
        """立即闭嘴：停播 + 放弃当前/待合成 + 丢弃未播字幕。"""
        self._gen += 1
        self._interrupted = True
        if self._pump_task is not None and not self._pump_task.done():
            try:
                self._pump_task.cancel()
            except Exception:
                pass
        if self._pending is not None:
            while True:
                try:
                    self._pending.get_nowait()
                except asyncio.QueueEmpty:
                    break
        self._player.interrupt()

    def clear_interrupt(self) -> None:
        """新一轮输出前复位打断标志。"""
        self._interrupted = False
        self._player.clear_interrupt()

    # ---------- 合成方法转发到 playback.py ----------

    async def speak(self, text: str, sfx: str = "") -> None:
        await _playback_mod.speak(self, text, sfx)

    async def _pump(self) -> None:
        await _playback_mod._pump(self)

    async def _synth_remote(self, texts: list, gen: int) -> None:
        await _playback_mod._synth_remote(self, texts, gen)

    async def _synth_stream(self, text: str, gen: int, sfx: str = "",
                            _retried: bool = False) -> None:
        await _playback_mod._synth_stream(self, text, gen, sfx, _retried=_retried)

    async def _synth_one(self, text: str, gen: int):
        return await _playback_mod._synth_one(self, text, gen)
