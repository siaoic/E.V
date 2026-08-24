"""Edge TTS 骨架：满足 slots.py TTSContract 的最小 stub。"""
from __future__ import annotations

from typing import Callable, Optional


def register(ctx) -> None:
    """register(ctx)：读取配置 → 实例化 EdgeTTSStub → 注册到 SlotName.tts。"""
    cfg = ctx.config
    impl_name: str = cfg.get("impl_name", "edge")
    voice: str = cfg.get("voice", "zh-CN-XiaoxiaoNeural")
    impl = EdgeTTSStub(name=impl_name, voice=voice)

    try:
        from ev.kernel.slots import SlotName
    except Exception as e:
        ctx.log("error", f"无法导入 SlotName: {e}")
        return
    if ctx.slots is None:
        ctx.log("warn", "ctx.slots 为空，跳过 tts 注册")
        return
    try:
        ctx.slots.register(SlotName.tts, impl_name, impl)
    except Exception as e:
        ctx.log("error", f"注册 tts 槽位失败 ({impl_name}): {e}")
        return
    ctx.log("ok", f"已注册 TTS(Edge): {impl_name} voice={voice!r}")


class EdgeTTSStub:
    """TTSContract 最小 stub：方法签名严格对齐 slots.py TTSContract。"""

    def __init__(self, name: str = "edge", voice: str = "zh-CN-XiaoxiaoNeural") -> None:
        self.name = name
        self.voice = voice
        self._running: bool = False
        self._interrupted: bool = False
        self._on_play: Optional[Callable] = None
        self._on_play_done: Optional[Callable] = None
        self._subtitle_cb: Optional[Callable] = None
        self._ref_audio: str = ""
        self._ref_text: str = ""
        self._ref_extras: str = ""

    # ---- 生命周期 ----
    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._running = False

    async def drain(self) -> None:
        # stub：无队列可排空
        return None

    async def speak(self, text: str) -> None:
        if not self._running or self._interrupted:
            return None
        if self._subtitle_cb is not None:
            try:
                self._subtitle_cb(text)
            except Exception:
                pass
        if self._on_play is not None:
            try:
                self._on_play(text)
            except Exception:
                pass
        if self._on_play_done is not None:
            try:
                self._on_play_done(text)
            except Exception:
                pass
        return None

    # ---- 中断 ----
    def interrupt(self) -> None:
        self._interrupted = True

    def clear_interrupt(self) -> None:
        self._interrupted = False

    # ---- 回调 ----
    def set_on_play_callback(self, cb: Optional[Callable]) -> None:
        self._on_play = cb

    def set_on_play_done_callback(self, cb: Optional[Callable]) -> None:
        self._on_play_done = cb

    def set_subtitle_callback(self, cb: Optional[Callable]) -> None:
        self._subtitle_cb = cb

    # ---- 参考音 ----
    def apply_ref(self, audio: str, text: str) -> None:
        self._ref_audio = audio
        self._ref_text = text

    def apply_ref_extras(self, extras: str) -> None:
        self._ref_extras = extras
