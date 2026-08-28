"""Slot 定义：SlotName 枚举 + SlotRegistry + 基础 Contract Protocol。

Slot = "插槽"：可插拔模块的挂载点。每个插槽可以注册多个实现，按 profile 激活某一个。
实现了 activate/deactivate 钩子、on_activate/on_deactivate 回调、注册冲突检查。
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Callable, Optional, Protocol, runtime_checkable


class SlotName(str, Enum):
    """所有约定的插槽名。字符串值 = profile.yaml 里 slots.* 的键。"""
    model       = "model"
    tts         = "tts"
    avatar      = "avatar"
    input       = "input"
    danmaku     = "danmaku"
    memory      = "memory"
    emotion     = "emotion"
    proactive   = "proactive"
    mcp         = "mcp"
    butler      = "butler"
    evolution   = "evolution"
    scheduler   = "scheduler"
    sandbox     = "sandbox"
    ui          = "ui"
    pet         = "pet"
    session     = "session"
    credentials = "credentials"


@runtime_checkable
class TTSContract(Protocol):
    name: str
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def drain(self) -> None: ...
    async def speak(self, text: str) -> None: ...
    def interrupt(self) -> None: ...
    def clear_interrupt(self) -> None: ...
    def set_on_play_callback(self, cb: Callable) -> None: ...
    def set_on_play_done_callback(self, cb: Callable) -> None: ...
    def set_subtitle_callback(self, cb: Callable) -> None: ...
    def apply_ref(self, audio: str, text: str) -> None: ...
    def apply_ref_extras(self, extras: str) -> None: ...


@runtime_checkable
class LLMContract(Protocol):
    """LLM slot 最小协议（=TR 13 + EchoLLM 声明的必需位）。

    start/stop 生命周期不强制：EchoLLM 等骨架实现无它们也能通过 isinstance，
    真实实现（LLMBrain 等）如有则 kernel 在启动/关闭时自动调用（用 hasattr 判定）。

    chat_stream 协议（v2）：
        yield (mode, text)，mode ∈ {"delta", "final"}
          - ("delta", text): 打字机流式实时显示（累加 buffer）
          - ("final", text): 一个完整可播分段（触发 TTS / 字幕 / 复读检测 / 事件）
    """
    name: str
    async def chat_stream(self, user_text: str, **kwargs): ...
    def push_turn_context(self, contexts) -> None: ...
    def reload_client(self) -> None: ...


@runtime_checkable
class AvatarContract(Protocol):
    """Avatar slot 最小协议：name + 9 个 connect/control 方法。

    GoodAvatar（TR 12.2）把这些方法标为「slots 必需」，故 slots.AvatarContract
    在此明确声明它们。生命周期 start/stop/on_activate/on_deactivate 是可选
    的 Kernel 级别钩子，不由 slots 强制要求（adapter.AvatarContract 可选扩展）。
    """
    name: str
    async def connect(self) -> None: ...
    async def ensure_connected(self) -> None: ...
    async def close(self) -> None: ...
    def on_event(self, event_name: str, handler: Callable) -> None: ...
    async def subscribe_event(self, event_name: str) -> bool: ...
    async def inject_parameters(self, params) -> None: ...
    async def trigger_motion(self, motion_file: str) -> bool: ...
    async def trigger_hotkey(self, hotkey_id: str, priority: str = "High") -> None: ...
    async def activate_expression(self, expr_file: str, active: bool = True) -> bool: ...


@runtime_checkable
class InputContract(Protocol):
    """最小 Input slot 协议：name + start/stop（同步/异步都允许）+ result_future。"""
    name: str
    def start(self) -> None: ...
    def stop(self) -> None: ...
    def result_future(self): ...


@runtime_checkable
class DanmakuContract(Protocol):
    """Danmaku slot 最小协议：只要求 name（其余接口在 adapter.contracts 扩展）。"""
    name: str


class SlotRegistry:
    def __init__(self) -> None:
        self._impls: dict[SlotName, dict[str, Any]] = {s: {} for s in SlotName}
        self._active: dict[SlotName, Optional[str]] = {s: None for s in SlotName}

    def register(self, slot: SlotName, impl_name: str, instance: Any) -> None:
        if slot not in self._impls:
            raise KeyError(f"Unknown slot: {slot}")
        if impl_name in self._impls[slot]:
            raise ValueError(f"Slot[{slot.value}] already has impl '{impl_name}'")
        self._impls[slot][impl_name] = instance

    def unregister(self, slot: SlotName, impl_name: str) -> None:
        if slot not in self._impls or impl_name not in self._impls[slot]:
            return
        if self._active[slot] == impl_name:
            self.deactivate(slot)
        del self._impls[slot][impl_name]

    def activate(self, slot: SlotName, impl_name: str) -> None:
        if slot not in self._impls:
            raise KeyError(f"Unknown slot: {slot}")
        if impl_name not in self._impls[slot]:
            raise KeyError(f"Slot[{slot.value}] no impl: '{impl_name}'")
        old_impl_name = self._active[slot]
        new_impl = self._impls[slot][impl_name]
        if old_impl_name is not None and old_impl_name != impl_name:
            old = self._impls[slot][old_impl_name]
            if hasattr(old, "on_deactivate"):
                old.on_deactivate()
        self._active[slot] = impl_name
        if old_impl_name != impl_name and hasattr(new_impl, "on_activate"):
            new_impl.on_activate()

    def deactivate(self, slot: SlotName) -> None:
        if self._active.get(slot) is None:
            return
        old = self._impls[slot][self._active[slot]]
        if hasattr(old, "on_deactivate"):
            old.on_deactivate()
        self._active[slot] = None

    def get(self, slot: SlotName):
        if slot not in self._impls or self._active[slot] is None:
            return None
        return self._impls[slot][self._active[slot]]

    def get_impl_names(self, slot: SlotName) -> set[str]:
        return set(self._impls.get(slot, {}).keys())

    def get_all(self, slot: SlotName) -> dict[str, Any]:
        """返回 {impl_name: instance}；等价 all_impls[name] 访问。"""
        impls = self._impls.get(slot, {})
        return {n: impls[n] for n in list(impls.keys())}

    def bindings(self) -> dict[str, Optional[str]]:
        return {s.value: self._active[s] for s in SlotName}

    def close_all(self) -> None:
        for s in list(self._active.keys()):
            if self._active[s] is not None:
                self.deactivate(s)


__all__ = [
    "SlotName", "SlotRegistry",
    "TTSContract", "LLMContract", "AvatarContract", "InputContract",
    "DanmakuContract",
]
