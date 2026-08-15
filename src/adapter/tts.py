"""TTS 适配器抽象基类：语音合成的标准契约。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Callable, Optional

from src.adapter.base import BaseAdapter


class BaseTTSAdapter(BaseAdapter):
    """语音合成适配器。

    本项目实现：src/tts/engine.py 的 TTSEngine（GPT-SoVITS HTTP 客户端）。
    更换 TTS 服务（如 VOICEVOX）只需新增实现类，上层调用不变。
    """

    name: str = "tts"

    @abstractmethod
    async def start(self) -> bool:
        """启动探测：服务可用返回 True，否则返回 False（语音降级纯字幕）。"""

    @abstractmethod
    async def speak(self, text: str) -> None:
        """入队合成并播放一段文本。"""

    @abstractmethod
    async def drain(self) -> None:
        """等待队列播完。"""

    @abstractmethod
    def interrupt(self) -> None:
        """打断当前播放。"""

    @abstractmethod
    def clear_interrupt(self) -> None:
        """复位打断标志（新一轮输出前调用）。"""

    @abstractmethod
    async def stop(self) -> None:
        """关闭客户端。"""

    @abstractmethod
    def set_on_play_callback(self, cb: Optional[Callable]) -> None:
        """设置音频播放回调（口型同步用）。"""

    @abstractmethod
    def set_subtitle_callback(self, cb: Optional[Callable]) -> None:
        """设置字幕推送回调。"""

    @abstractmethod
    def apply_ref(self, audio: str, text: str) -> None:
        """热更新主参考音频 / 文本。"""

    @abstractmethod
    def apply_ref_extras(self, extras: str) -> None:
        """热更新辅助参考音频（多条用 | 分隔）。"""
