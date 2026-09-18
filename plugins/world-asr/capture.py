"""能量 VAD 麦克风分段采集。

设计要点：
- 采集与 VAD 判定全部发生在 sounddevice 的音频回调线程里（每帧只做一次
  RMS 计算和列表追加，足够快，不会掉帧），分段完成后经
  ``loop.call_soon_threadsafe`` 交回 asyncio 侧，音频线程不做任何阻塞调用；
- 「说话开始」带上 ``pre_roll_seconds`` 的前置缓冲，避免吃掉句首音节；
- 分段结束条件：安静超过 ``silence_seconds``（句间停顿）或总时长达到
  ``max_segment_seconds``（长句强制切分）；
- ``on_segment`` 收到的是完整 WAV 字节（PCM_16），直接可喂给语音识别。
"""

from __future__ import annotations

import asyncio
import io
import threading
import time
import wave
from typing import Callable, List, Optional

import numpy as np
import sounddevice as sd
import soundfile as sf


class EnergyVadMic:
    """基于能量门限的麦克风分段采集器。"""

    def __init__(
        self,
        *,
        device: Optional[int],
        sample_rate: int,
        silence_seconds: float,
        silence_threshold_rms: float,
        min_speech_seconds: float,
        max_segment_seconds: float,
        on_segment: Callable[[bytes, float], None],
        loop: asyncio.AbstractEventLoop,
        logger,
    ) -> None:
        """构建采集器；参数含义见插件 ``config.toml`` 注释。

        Args:
            device: sounddevice 输入设备索引；``None`` 表示系统默认输入设备。
            on_segment: 分段完成回调，参数为 ``(wav_bytes, 采集开始的单调时刻)``，
                在音频线程被调用前先被调度回事件循环线程。
            loop: 用于把分段结果从音频线程安全投回的 asyncio 事件循环。
            logger: 结构化日志器。
        """

        self._device = device
        self._sample_rate = sample_rate
        self._frame_seconds = 0.03
        self._frame_size = int(sample_rate * self._frame_seconds)
        self._silence_frames_needed = max(int(silence_seconds / self._frame_seconds), 1)
        self._silence_threshold_rms = float(silence_threshold_rms)
        self._min_speech_frames = max(int(min_speech_seconds / self._frame_seconds), 1)
        self._max_segment_frames = max(int(max_segment_seconds / self._frame_seconds), 1)
        self._pre_roll_frames = max(int(0.3 / self._frame_seconds), 1)
        self._on_segment = on_segment
        self._loop = loop
        self._logger = logger

        self._stream: Optional[sd.InputStream] = None
        # 音频线程独占，asyncio 侧只通过事件/标志读取快照，故仅用锁保护统计计数
        self._lock = threading.Lock()
        self._speaking = False
        self._segment: List[np.ndarray] = []
        self._segment_frames = 0
        self._speech_frames = 0
        self._silence_frames = 0
        self._pre_roll: List[np.ndarray] = []
        self._segment_started_at = 0.0
        self._segments_produced = 0

    @property
    def segments_produced(self) -> int:
        """已产出的语音分段总数（跨连接累计）。"""

        with self._lock:
            return self._segments_produced

    def status_text(self) -> str:
        """当前采集状态的一句话描述（供 world_observe 快照用）。"""

        with self._lock:
            speaking = self._speaking
            produced = self._segments_produced
        return ("正在说话" if speaking else "安静") + f"，已产出 {produced} 段语音"

    async def start(self) -> None:
        """打开输入流开始采集。

        Raises:
            RuntimeError: 设备不存在或打开失败时抛出——麦克风拿不到就没有
                这个世界，必须让加载失败并暴露原因，而不是安静空转。
        """

        try:
            self._stream = sd.InputStream(
                device=self._device,
                channels=1,
                samplerate=self._sample_rate,
                dtype="int16",
                blocksize=self._frame_size,
                callback=self._on_audio_frame,
            )
            self._stream.start()
        except Exception as exc:
            self._stream = None
            raise RuntimeError(f"打开麦克风输入流失败: {exc}") from exc
        self._logger.info(
            f"麦克风采集已启动: device={'默认输入' if self._device is None else self._device} "
            f"sample_rate={self._sample_rate} frame={self._frame_seconds * 1000:.0f}ms"
        )

    async def stop(self) -> None:
        """关闭输入流；未关闭时重复调用无副作用。"""

        stream = self._stream
        self._stream = None
        if stream is None:
            return
        try:
            stream.stop()
            stream.close()
        except Exception:
            self._logger.exception("关闭麦克风输入流失败")
        with self._lock:
            self._speaking = False
            self._segment = []
            self._segment_frames = 0
            self._speech_frames = 0
            self._silence_frames = 0
            self._pre_roll = []

    # ------------------------------------------------------------------ 音频线程侧

    def _on_audio_frame(self, indata: np.ndarray, frames: int, time_info, status) -> None:
        """sounddevice 回调：逐帧 RMS 判定 + VAD 状态机（禁止阻塞）。"""

        del time_info
        if status:
            # 输入溢出等状态走 stderr 日志，不打断采集
            print(f"[world-asr] 音频输入状态异常: {status}", flush=True)
        if self._stream is None:
            return

        frame = indata[:, 0].astype(np.float32)
        rms = float(np.sqrt(np.mean(frame**2))) if frame.size else 0.0

        with self._lock:
            self._feed_frame(frame, rms)

    def _feed_frame(self, frame: np.ndarray, rms: float) -> None:
        """VAD 状态机单步推进；调用方持有 ``_lock``。"""

        loud = rms >= self._silence_threshold_rms

        if not self._speaking:
            self._pre_roll.append(frame)
            if len(self._pre_roll) > self._pre_roll_frames:
                self._pre_roll.pop(0)
            if loud:
                self._speaking = True
                self._segment = list(self._pre_roll)
                self._segment_frames = len(self._segment)
                self._speech_frames = 1
                self._silence_frames = 0
                self._segment_started_at = time.monotonic()
                self._pre_roll = []
            return

        self._segment.append(frame)
        self._segment_frames += 1
        if loud:
            self._speech_frames += 1
            self._silence_frames = 0
        else:
            self._silence_frames += 1

        reached_silence = self._silence_frames >= self._silence_frames_needed
        reached_cap = self._segment_frames >= self._max_segment_frames
        if reached_silence or reached_cap:
            self._flush_segment()

    def _flush_segment(self) -> None:
        """结算当前分段；有效（足够长）的分段编码成 WAV 后交回事件循环。"""

        self._speaking = False
        segment_frames = self._segment_frames
        speech_frames = self._speech_frames
        started_at = self._segment_started_at
        segment = self._segment
        self._segment = []
        self._segment_frames = 0
        self._speech_frames = 0
        self._silence_frames = 0

        # 去掉句尾静音帧，只保留语音与收尾缓冲
        keep = segment_frames - self._silence_frames
        if speech_frames < self._min_speech_frames or keep <= 0:
            return
        trimmed = segment[:keep]
        self._segments_produced += 1

        wav_bytes = encode_wav(trimmed, sample_rate=self._sample_rate)
        loop = self._loop
        callback = self._on_segment
        loop.call_soon_threadsafe(callback, wav_bytes, started_at)


def encode_wav(frames: List[np.ndarray], *, sample_rate: int) -> bytes:
    """把 int16 单声道帧列表编码成 WAV 字节（PCM_16）。"""

    pcm = np.concatenate(frames).astype(np.int16)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(pcm.tobytes())
    # soundfile 校验一遍可读性，尽早暴露编码问题
    data, _ = sf.read(io.BytesIO(buffer.getvalue()), dtype="int16")
    if data.size == 0:
        raise RuntimeError("编码后的语音分段为空")
    return buffer.getvalue()


def resolve_input_device(name: str, logger) -> Optional[int]:
    """按名称解析输入设备索引；留空返回默认设备。

    Raises:
        RuntimeError: 配置的设备名不存在时抛出，并列出全部可用输入设备。
    """

    wanted = str(name or "").strip()
    if not wanted:
        return None

    devices = sd.query_devices()
    for index, info in enumerate(devices):
        if info.get("max_input_channels", 0) <= 0:
            continue
        if wanted.casefold() in str(info.get("name", "")).casefold():
            logger.info(f"麦克风输入设备匹配: index={index} name={info.get('name')}")
            return int(index)

    available = [
        f"{i}: {info.get('name')}"
        for i, info in enumerate(devices)
        if info.get("max_input_channels", 0) > 0
    ]
    raise RuntimeError(
        f"找不到麦克风输入设备「{wanted}」，可用输入设备：{'；'.join(available) or '（无）'}"
    )
