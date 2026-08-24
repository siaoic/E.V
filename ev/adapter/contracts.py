"""适配器层 Contract Protocol 超集（=slots 同名契约 + adapter 层扩展）。

每个 adapter.Contract 都是同名 slots.Contract 的超集：
- slots 层声明最小必需接口（Good* 里标记「slots 必需」的方法/属性）。
- adapter 层只追加 Good* 中标记「adapter 扩展」的接口，不添加其他可选扩展
  （例如 aread / reload_model 等），否则 runtime_checkable 会把这些方法
  视作必需，导致 TR 12 的 GoodAvatar/GoodInput 不通过 isinstance 检查。
"""
from __future__ import annotations
from typing import Any, AsyncIterator, Awaitable, Callable, Protocol, runtime_checkable

from ev.kernel.slots import (
    AvatarContract as _SlotAvatar,
    InputContract as _SlotInput,
    LLMContract as _SlotLLM,
    TTSContract as _SlotTTS,
    DanmakuContract as _SlotDanmaku,
)


@runtime_checkable
class TTSContract(_SlotTTS, Protocol):
    """adapter 扩展：synthesize / synthesize_stream。"""

    async def synthesize(self, text: str) -> bytes: ...
    async def synthesize_stream(self, text: str) -> AsyncIterator[bytes]:
        if False:
            yield b""
        return
        yield b""


@runtime_checkable
class LLMContract(_SlotLLM, Protocol):
    """adapter 扩展：agenerate / agenerate_stream。"""

    async def agenerate(self, messages: list[dict], **kwargs) -> str: ...
    async def agenerate_stream(self, messages: list[dict], **kwargs) -> AsyncIterator[str]:
        if False:
            yield ""
        return
        yield ""


@runtime_checkable
class AvatarContract(_SlotAvatar, Protocol):
    """adapter 扩展（GoodAvatar 显式提供）：running/is_connected/wait_fully_ready/
    send_text/set_emotion。"""

    @property
    def running(self) -> bool: ...
    @property
    def is_connected(self) -> bool: ...
    async def wait_fully_ready(self, timeout: float = 30.0) -> bool: ...
    async def send_text(self, text: str) -> None: ...
    async def set_emotion(self, emotion: str, duration: float = 2.0) -> None: ...


@runtime_checkable
class InputContract(_SlotInput, Protocol):
    """adapter 扩展（GoodInput 显式提供）：running + async 迭代协议。"""

    @property
    def running(self) -> bool: ...
    def __aiter__(self): ...
    async def __anext__(self) -> str: ...


@runtime_checkable
class DanmakuContract(_SlotDanmaku, Protocol):
    """adapter 扩展（GoodDM 显式提供）：running/connect/disconnect/on_message。"""

    @property
    def running(self) -> bool: ...
    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...
    def on_message(self, handler: Callable) -> None: ...


__all__ = [
    "TTSContract",
    "LLMContract",
    "AvatarContract",
    "InputContract",
    "DanmakuContract",
]
