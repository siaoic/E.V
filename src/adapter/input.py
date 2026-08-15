"""输入源适配器抽象基类：语音识别等输入源的标准契约。"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod

from src.adapter.base import BaseAdapter


class BaseInputAdapter(BaseAdapter):
    """输入源适配器（语音识别等）。

    本项目实现：src/asr/stt.py 的 STTEngine（SiliconFlow 云端转写）。
    键盘输入 / 弹幕由内核主循环直接处理（无需抽象）。
    """

    name: str = "input"

    @abstractmethod
    def start(self) -> None:
        """启动采集（后台线程录音 / 识别）。"""

    @abstractmethod
    def stop(self) -> None:
        """停止采集。"""

    @abstractmethod
    def result_future(self) -> "asyncio.Future":
        """返回下一个识别结果的 future；完成时 result() 为 (text, speech_seconds)。"""
