# -*- coding: utf-8 -*-
"""MaiBot 端 TTS 朗读客户端。

订阅 MaiBot 对外发送的文本回复，把它们交给独立的 GSV-TTS-Lite 流式服务
（`tts_service/app.py`）逐 chunk 合成，边合成边用 sounddevice 播放到指定
（虚拟）扬声器设备。

- 通过 SSE 从服务端接收 `int16` 单声道 PCM，解码后阻塞写入 OutputStream。
- 整个「收流 + 播放」在一个独立线程的独立事件循环里完成，不阻塞 MaiBot 主循环。
- 各条回复经内部队列串行朗读，避免多条同时播造成重叠。
- 默认关闭；仅当 `config/tts_config.toml` 中 `enabled = true` 且服务可达时才有行为。
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import threading
from pathlib import Path

import numpy as np
import tomllib

logger = logging.getLogger("tts_player")

# 数字逐位朗读表：弹幕语境下 666/233 应按位读作"六六六/二三三"，
# gsv_tts 无法处理纯数字文本（产不出音素段会直接报错），因此发送前转写
_DIGIT_READINGS = "零一二三四五六七八九"
_DIGIT_TRANSLATION = str.maketrans(
    "0123456789０１２３４５６７８９",
    _DIGIT_READINGS + _DIGIT_READINGS,
)

# 可朗读字符：汉字（含扩展A区）/ 日文假名 / 韩文谚文 / 拉丁字母
_SPEAKABLE_PATTERN = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf\u3040-\u30ff\uac00-\ud7afA-Za-z]")


def _normalize_speak_text(text: str) -> str | None:
    """把待朗读文本归一化为可合成的形式；无法合成时返回 None。

    - 数字（含全角）逐位转写为中文读音，避免 gsv_tts 对纯数字文本报
      "no valid segments"；
    - 清洗后必须仍含有至少一个可朗读字符（汉字/假名/谚文/拉丁字母），
      否则视为不可朗读（纯标点、纯 emoji 等）。
    """
    normalized = text.translate(_DIGIT_TRANSLATION)
    if not _SPEAKABLE_PATTERN.search(normalized):
        return None
    return normalized

# 配置文件路径，可用环境变量 TTS_CONFIG 覆盖
_CONFIG_PATH = Path(
    os.environ.get("TTS_CONFIG", str(Path(__file__).resolve().parents[3] / "config" / "tts_config.toml"))
)


def load_config() -> dict:
    """读取 TTS 朗读配置文件，缺失或解析失败时返回未启用配置。"""
    if not _CONFIG_PATH.exists():
        return {"enabled": False}
    with open(_CONFIG_PATH, "rb") as f:
        return tomllib.load(f)


class TtsPlayer:
    """接收 AI 回复文本流式朗读到指定音频输出设备。"""

    def __init__(
        self,
        config: dict,
        *,
        meta_resolver=None,
        param_resolver=None,
    ) -> None:
        """初始化播放器。

        Args:
            config: 基础配置（enabled/base_url/音色参数等）。
            meta_resolver: 可选的总开关解析函数，返回是否朗读；注入后取代默认
                的「优先读 WebUI 主配置」逻辑，用于直播等独立播放路径。
            param_resolver: 可选的音色/设备参数解析函数；注入后取代默认的
                「优先读 WebUI 主配置」参数解析逻辑。
        """
        self._enabled = bool(config.get("enabled", False))
        self._base_url = str(config.get("base_url") or "").rstrip("/")
        self._spk_audio = config.get("spk_audio") or None
        self._prompt_audio = config.get("prompt_audio") or None
        self._prompt_text = config.get("prompt_text") or None
        self._stream_chunk = int(config.get("stream_chunk") or 25)
        self._overlap_len = int(config.get("overlap_len") or 5)
        # sounddevice 输出设备：可为设备名或索引；None 表示系统默认设备
        self._device = config.get("output_device") or None
        # 在收到 meta 前的兜底采样率（GSV-TTS-Lite 默认 32000）
        self._default_sample_rate = int(config.get("sample_rate") or 32000)
        self._meta_resolver = meta_resolver
        self._param_resolver = param_resolver

        self._queue: asyncio.Queue = asyncio.Queue()
        self._consumer_task: asyncio.Task | None = None

        # 音频包络订阅者（W4 口型联动等）：每个 PCM chunk 解码后回调
        # listener(rms, sample_rate)。回调在播放线程发生，订阅方必须自己保证线程安全；
        # 回调抛异常只记日志，绝不影响播放本身。
        self._envelope_listeners: list = []

    def add_envelope_listener(self, listener) -> None:
        """订阅播放包络；``listener(rms, sample_rate)`` 会在每个 PCM chunk 后被调用。"""
        if listener not in self._envelope_listeners:
            self._envelope_listeners.append(listener)

    def remove_envelope_listener(self, listener) -> None:
        """取消包络订阅。"""
        if listener in self._envelope_listeners:
            self._envelope_listeners.remove(listener)

    def _resolve_enabled(self) -> bool:
        """读取是否朗读的本机开关。

        注入过 meta_resolver 时（如直播独立路径）完全按其决定，
        不读取 WebUI 主配置；否则读取 WebUI 的
        `chat.tts_read.enable_tts_read` 作为唯一控制来源（便于不重启即时生效），
        当主配置不可达时才回退到 `tts_config.toml` 的 `enabled`。
        """
        if self._meta_resolver is not None:
            return bool(self._meta_resolver())
        try:
            from src.config.config import global_config

            return bool(global_config.chat.tts_read.enable_tts_read)
        except Exception:
            return self._enabled

    def _resolve_param(self, name: str):
        """解析参考音频等参数：优先取 WebUI 主配置，其次回退 `tts_config.toml`。

        空字符串表示使用服务端默认，因此返回 None 时不传给服务端。
        注入过 param_resolver 时（如直播独立路径）完全按其决定。
        """
        if self._param_resolver is not None:
            return self._param_resolver(name)
        try:
            from src.config.config import global_config

            val = getattr(global_config.chat.tts_read, name, None)
            if val:
                return val
        except Exception:
            pass
        return getattr(self, f"_{name}", None) or None

    async def speak(self, text: str) -> None:
        """把回复文本加入朗读队列（会话内非阻塞，立即返回）。"""
        text = _normalize_speak_text((text or "").strip())
        if not self._resolve_enabled() or not text:
            if not text:
                logger.debug("文本无可朗读字符，跳过合成")
            return
        # 惰性启动消费协程；speak 在事件循环内被调用，可直接创建任务
        if self._consumer_task is None or self._consumer_task.done():
            self._consumer_task = asyncio.create_task(self._consume())
        self._queue.put_nowait(text)

    async def _consume(self) -> None:
        """串行消费朗读队列。"""
        while True:
            text = await self._queue.get()
            try:
                await self._play(text)
            except Exception:
                logger.exception("TTS 流式朗读失败，文本=%r", text[:40])
            finally:
                self._queue.task_done()

    def clear_queue(self) -> None:
        """清空尚未朗读的文本，并停止后续播放。

        用于「关闭直播模式」等需要立即静默的场景：清掉仍在队列里等待
        播放的回复（正在实际播放的单条无法中断，会播完即停）。之后新的
        speak 因开关已关闭也不会再入队。
        """
        while True:
            try:
                self._queue.get_nowait()
                self._queue.task_done()
            except asyncio.QueueEmpty:
                break
        if self._consumer_task is not None:
            self._consumer_task.cancel()
            self._consumer_task = None

    async def _play(self, text: str) -> None:
        """在同线程独立事件循环里收流并播放，避免阻塞 MaiBot 主事件循环。"""
        logger.info("开始朗读（%d 字）：%s", len(text), text)
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._play_sync, text)

    def _play_sync(self, text: str) -> None:
        """以独立线程执行：请求 SSE 流，边合成边用 sounddevice 阻塞播放。"""

        # aiohttp 允许重复键，因此只用列表承载多个 spk_audio_extra
        params = [("text", text), ("stream_chunk", str(self._stream_chunk)), ("overlap_len", str(self._overlap_len))]

        # 主参考音频（作风格参考）及其文本，优先取 WebUI 配置，留空则服务端用默认
        spk_main = self._resolve_param("spk_audio")
        spk_text = self._resolve_param("spk_audio_text")
        if spk_main:
            params.append(("spk_audio", spk_main))
            if spk_text:
                params.append(("spk_audio_text", spk_text))

        # 追加音色参考（可多个、无需文本）
        spk_extra = self._resolve_param("spk_audio_additional") or []
        if isinstance(spk_extra, str):
            spk_extra = [spk_extra]
        for p in spk_extra:
            if p:
                params.append(("spk_audio_extra", p))

        # 朗读输出设备：优先取 WebUI 配置，否则回退 tts_config.toml 的 output_device
        device = self._resolve_param("output_device") or self._device

        url = f"{self._base_url}/tts/stream"

        # 收流与播放放在同一个独立事件循环里完成；sounddevice 阻塞播放也执行在该线程，
        # 不会影响 MaiBot 主事件循环
        asyncio.run(self._collect_and_play(url, params, device))

    async def _collect_and_play(self, url: str, params, device) -> None:
        """请求 SSE 流，逐 chunk 解码 PCM 后阻塞写入 sounddevice。"""
        import aiohttp
        import sounddevice as sd

        # 把配置里的含糊设备名消歧为唯一索引，避免同名设备（多 host api）报歧义
        device = _resolve_output_device(sd, device)

        stream: sd.OutputStream | None = None
        sample_rate = self._default_sample_rate
        pending = bytearray()
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=None, connect=10)
            ) as session:
                async with session.get(url, params=params) as resp:
                    resp.raise_for_status()
                    # iter_chunked 会等凑满指定字节才吐数据（首个 SSE 事件被憋在缓冲里，
                    # 观众听首字会白等 1-2s）；iter_any 有多少吐多少，真正边收边播
                    async for raw in resp.content.iter_any():
                        pending.extend(raw)
                        while True:
                            idx = pending.find(b"\n\n")
                            if idx < 0:
                                break
                            block = bytes(pending[:idx])
                            del pending[: idx + 2]
                            if not block.strip():
                                continue
                            event, data = _parse_sse_block(block)
                            if event == "meta":
                                meta = json.loads(data)
                                sample_rate = int(meta.get("sample_rate", sample_rate))
                            elif event == "chunk":
                                pcm16 = base64.b64decode(data)
                                pcm = (
                                    np.frombuffer(pcm16, dtype=np.int16).astype(np.float32)
                                    / 32768.0
                                )
                                if stream is None:
                                    stream = sd.OutputStream(
                                        samplerate=sample_rate,
                                        channels=1,
                                        dtype="float32",
                                        device=device,
                                    )
                                    stream.start()
                                self._emit_envelope(pcm, sample_rate)
                                stream.write(pcm.reshape(-1, 1))
                            elif event == "error":
                                raise RuntimeError(f"服务端语音合成出错: {data}")
                            elif event == "done":
                                return
        finally:
            if stream is not None:
                stream.stop()
                stream.close()

    def _emit_envelope(self, pcm: "np.ndarray", sample_rate: int) -> None:
        """把本 chunk 的 RMS 包络推给订阅者；订阅者异常不影响播放。"""
        if not self._envelope_listeners:
            return
        rms = float(np.sqrt(np.mean(pcm**2))) if pcm.size else 0.0
        for listener in list(self._envelope_listeners):
            try:
                listener(rms, sample_rate)
            except Exception:
                logger.exception("TTS 包络订阅者回调失败，已跳过该订阅者")


def _resolve_output_device(sd, device):
    """把含糊的设备名消歧为唯一索引。

    sounddevice 按「名称」匹配输出设备时，同名设备若同时出现在 MME /
    DirectSound / WASAPI 等多个 host api 中，会因无法确定唯一设备而报
    ``Multiple output devices found`` 错误（例如 ``扬声器 (Realtek(R)
    Audio)`` 在 Windows 上常同时注册多个 host api 副本）。

    这里在向 sounddevice 传参前把名称解析为唯一数字索引：
    - 已是指针（None=默认 / int=索引）则原样返回；
    - 名称唯一则直接取该索引；
    - 名称匹配多个时优先选 MME / DirectSound（host api 序号小的）：
     这两个 host api 的 PortAudio 驱动会做采样率转换，能兼容 GSV-TTS 的
     32000Hz 输出；而 WASAPI / WDM-KS 对采样率限制严格，可能拒绝 32k；
    - 无匹配时原样返回，交由 sounddevice 背后的错误信息暴露。
    """
    if device is None or isinstance(device, int):
        return device

    hostapis = {i: hint["name"] for i, hint in enumerate(sd.query_hostapis())}
    candidates = []
    for index, info in enumerate(sd.query_devices()):
        if info["name"] == device and int(info["max_output_channels"]) > 0:
            candidates.append((index, int(info["hostapi"])))
    if not candidates:
        return device
    # 同名多候选：选 host api 序号最小的（MME=0 / DirectSound=1 优先于 WASAPI=2 / WDM-KS=3）
    candidates.sort(key=lambda item: item[1])
    return candidates[0][0]


def _parse_sse_block(block: bytes) -> tuple[str, str]:
    """解析单个 SSE 事件块，返回 (事件名, data 内容)。"""
    event = ""
    data = ""
    for line in block.decode("utf-8", "ignore").splitlines():
        line = line.strip()
        if line.startswith("event:"):
            event = line[len("event:") :].strip()
        elif line.startswith("data:"):
            data += line[len("data:") :].strip()
    return event, data