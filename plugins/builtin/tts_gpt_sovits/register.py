"""GPT-SoVITS TTS 骨架：满足 TTSContract + 附带 SubtitleServerWrapper。"""
from __future__ import annotations

from typing import Callable, Optional


def register(ctx) -> None:
    cfg = ctx.config
    impl_name: str = cfg.get("impl_name", "gptsovits")
    api_url: str = cfg.get("api_url", "http://127.0.0.1:9880")
    ref_audio: str = cfg.get("ref_audio", "")
    ref_text: str = cfg.get("ref_text", "")

    impl = GPTSovitsTTSStub(
        name=impl_name,
        api_url=api_url,
        ref_audio=ref_audio,
        ref_text=ref_text,
    )

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
    ctx.log("ok", f"已注册 TTS(GPT-SoVITS): {impl_name} api={api_url!r}")


class SubtitleServerWrapper:
    """SubtitleServer 占位：SSE 推流状态（非 Protocol，仅辅助配置验证）。"""

    def __init__(self, enabled: bool = True, port: int = 7861) -> None:
        self.enabled = enabled
        self.port = port
        self.clients: int = 0
        self.queue: list[str] = []

    def push(self, text: str) -> None:
        self.queue.append(text)

    def start(self) -> None:
        return None

    def stop(self) -> None:
        return None


class GPTSovitsTTSStub:
    """TTSContract 严格 stub；附 self.subtitle 用于验证 gptsovits 配置生效。"""

    def __init__(
        self,
        name: str = "gptsovits",
        api_url: str = "http://127.0.0.1:9880",
        ref_audio: str = "",
        ref_text: str = "",
    ) -> None:
        self.name = name
        self.api_url = api_url
        self._ref_audio = ref_audio
        self._ref_text = ref_text
        self._ref_extras: str = ""
        self._running: bool = False
        self._interrupted: bool = False
        self._on_play: Optional[Callable] = None
        self._on_play_done: Optional[Callable] = None
        self._subtitle_cb: Optional[Callable] = None
        # 非 Protocol 字段：测试可读取以验证配置
        self.subtitle = SubtitleServerWrapper(enabled=True, port=7861)

    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._running = False

    async def drain(self) -> None:
        return None

    async def speak(self, text: str) -> None:
        if not self._running or self._interrupted:
            return None
        if self._subtitle_cb is not None:
            try:
                self._subtitle_cb(text)
            except Exception:
                pass
        if self.subtitle is not None:
            try:
                self.subtitle.push(text)
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

    def interrupt(self) -> None:
        self._interrupted = True

    def clear_interrupt(self) -> None:
        self._interrupted = False

    def set_on_play_callback(self, cb: Optional[Callable]) -> None:
        self._on_play = cb

    def set_on_play_done_callback(self, cb: Optional[Callable]) -> None:
        self._on_play_done = cb

    def set_subtitle_callback(self, cb: Optional[Callable]) -> None:
        self._subtitle_cb = cb

    def apply_ref(self, audio: str, text: str) -> None:
        self._ref_audio = audio
        self._ref_text = text

    def apply_ref_extras(self, extras: str) -> None:
        self._ref_extras = extras
