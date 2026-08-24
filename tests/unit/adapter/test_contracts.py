"""TR 12.1 / 12.2 / 12.3 / 12.4 — adapter.contracts 4 个 Protocol 的最小实现校验。

同时验证与 src.core.slots 同名 Contract 的一致性：
  一个满足 adapter.Contract 的对象，应当**同时**满足 slots.Contract（因为 adapter
  是 slots 的超集）。
"""
from __future__ import annotations

import asyncio
from typing import AsyncIterator, Awaitable, Callable

import pytest


# ---------------------------------------------------------------------------
# TR 12.1: TTSContract
# ---------------------------------------------------------------------------
def test_tts_contract_minimal():
    from ev.adapter.contracts import TTSContract

    class GoodTTS:
        name = "ok-tts"

        # slots 必需
        async def start(self) -> None: pass
        async def stop(self) -> None: pass
        async def drain(self) -> None: pass
        async def speak(self, text: str) -> None: pass
        def interrupt(self) -> None: pass
        def clear_interrupt(self) -> None: pass
        def set_on_play_callback(self, cb) -> None: pass
        def set_on_play_done_callback(self, cb) -> None: pass
        def set_subtitle_callback(self, cb) -> None: pass
        def apply_ref(self, audio: str, text: str) -> None: pass
        def apply_ref_extras(self, extras: str) -> None: pass

        # adapter 扩展
        async def synthesize(self, text: str) -> bytes: return b""
        async def synthesize_stream(self, text: str) -> AsyncIterator[bytes]:
            if False:
                yield b""
            return
            yield b""  # unreachable：强制 async generator

    assert isinstance(GoodTTS(), TTSContract)

    # Bad1：缺 slots 必需的 speak
    class Bad1:
        name = "bad1"
        async def start(self): pass
        async def stop(self): pass
        async def drain(self): pass
        def interrupt(self): pass
        def clear_interrupt(self): pass
        def set_on_play_callback(self, cb): pass
        def set_on_play_done_callback(self, cb): pass
        def set_subtitle_callback(self, cb): pass
        def apply_ref(self, a, t): pass
        def apply_ref_extras(self, e): pass
        async def synthesize(self, t): return b""
        async def synthesize_stream(self, t):
            if False: yield b""
            return
            yield b""

    assert not isinstance(Bad1(), TTSContract)

    # Bad2：缺 name
    class Bad2:
        async def start(self): pass
        async def stop(self): pass
        async def drain(self): pass
        async def speak(self, t): pass
        def interrupt(self): pass
        def clear_interrupt(self): pass
        def set_on_play_callback(self, cb): pass
        def set_on_play_done_callback(self, cb): pass
        def set_subtitle_callback(self, cb): pass
        def apply_ref(self, a, t): pass
        def apply_ref_extras(self, e): pass
        async def synthesize(self, t): return b""
        async def synthesize_stream(self, t):
            if False: yield b""
            return
            yield b""

    assert not isinstance(Bad2(), TTSContract)

    # ---- 与 slots.TTSContract 的一致性断言 ----
    from ev.kernel.slots import TTSContract as SlotsTTS

    good = GoodTTS()
    # adapter 超集 ⇒ 满足 adapter 的对象也必须满足 slots
    assert isinstance(good, TTSContract) is isinstance(good, SlotsTTS)
    assert isinstance(good, SlotsTTS) is True


# ---------------------------------------------------------------------------
# TR 12.2: AvatarContract
# ---------------------------------------------------------------------------
def test_avatar_contract_minimal():
    from ev.adapter.contracts import AvatarContract

    class GoodAvatar:
        name = "vts"

        # slots 必需
        async def connect(self) -> None: pass
        async def ensure_connected(self) -> None: pass
        async def close(self) -> None: pass
        def on_event(self, event_name: str, handler: Callable) -> None: pass
        async def subscribe_event(self, event_name: str) -> bool: return True
        async def inject_parameters(self, params) -> None: pass
        async def trigger_motion(self, motion_file: str) -> bool: return True
        async def trigger_hotkey(self, hotkey_id: str, priority: str = "High") -> None: pass
        async def activate_expression(self, expr_file: str, active: bool = True) -> bool: return True

        # adapter 扩展（属性/方法）
        @property
        def running(self) -> bool: return False
        @property
        def is_connected(self) -> bool: return False
        async def wait_fully_ready(self, timeout: float = 30.0) -> bool: return True
        async def send_text(self, text: str) -> None: pass
        async def set_emotion(self, emotion: str, duration: float = 2.0) -> None: pass

    assert isinstance(GoodAvatar(), AvatarContract)

    # Bad：缺 slots 必需的 ensure_connected
    class Bad:
        name = "bad"
        async def connect(self): pass
        async def close(self): pass
        def on_event(self, e, h): pass
        async def subscribe_event(self, e): return True
        async def inject_parameters(self, p): pass
        async def trigger_motion(self, m): return True
        async def trigger_hotkey(self, h, p="High"): pass
        async def activate_expression(self, e, a=True): return True
        @property
        def running(self): return False
        @property
        def is_connected(self): return False
        async def wait_fully_ready(self, t=30): return True
        async def send_text(self, t): pass
        async def set_emotion(self, e, d=2): pass

    assert not isinstance(Bad(), AvatarContract)

    # ---- 与 slots.AvatarContract 的一致性断言 ----
    from ev.kernel.slots import AvatarContract as SlotsAvatar

    good = GoodAvatar()
    assert isinstance(good, AvatarContract) is isinstance(good, SlotsAvatar)
    assert isinstance(good, SlotsAvatar) is True


# ---------------------------------------------------------------------------
# TR 12.3: InputContract
# ---------------------------------------------------------------------------
def test_input_contract_minimal():
    from ev.adapter.contracts import InputContract

    class GoodInput:
        name = "cli"

        # slots 必需（注意：start/stop 是 sync，result_future 返回 asyncio.Future）
        def start(self) -> None: pass
        def stop(self) -> None: pass
        def result_future(self) -> asyncio.Future:
            loop = asyncio.get_event_loop_policy().get_event_loop()
            f = loop.create_future()
            f.set_result("hello")
            return f

        # adapter 扩展
        @property
        def running(self) -> bool: return False
        def __aiter__(self): return self
        async def __anext__(self) -> str:
            raise StopAsyncIteration

    assert isinstance(GoodInput(), InputContract)

    # Bad：缺 slots 必需的 result_future
    class Bad:
        name = "bad"
        def start(self): pass
        def stop(self): pass
        @property
        def running(self): return False
        def __aiter__(self): return self
        async def __anext__(self) -> str:
            raise StopAsyncIteration

    assert not isinstance(Bad(), InputContract)

    # ---- 与 slots.InputContract 的一致性断言 ----
    from ev.kernel.slots import InputContract as SlotsInput

    good = GoodInput()
    assert isinstance(good, InputContract) is isinstance(good, SlotsInput)
    assert isinstance(good, SlotsInput) is True


# ---------------------------------------------------------------------------
# TR 12.4: DanmakuContract
# ---------------------------------------------------------------------------
def test_danmaku_contract_minimal():
    from ev.adapter.contracts import DanmakuContract

    class GoodDM:
        name = "bili"

        # adapter 扩展（slots 目前仅要求 name）
        @property
        def running(self) -> bool: return False
        async def connect(self) -> None: pass
        async def disconnect(self) -> None: pass
        def on_message(self, handler):
            self._h = handler

    assert isinstance(GoodDM(), DanmakuContract)

    # Bad1：缺 name（slots 最小要求不满足）
    class Bad1:
        @property
        def running(self): return False
        async def connect(self): pass
        async def disconnect(self): pass
        def on_message(self, h): self._h = h

    assert not isinstance(Bad1(), DanmakuContract)

    # Bad2：有 name，但缺 adapter 要求的 on_message
    class Bad2:
        name = "bad2"
        @property
        def running(self): return False
        async def connect(self): pass
        async def disconnect(self): pass

    assert not isinstance(Bad2(), DanmakuContract)

    # ---- 与 slots.DanmakuContract 的一致性断言 ----
    from ev.kernel.slots import DanmakuContract as SlotsDM

    good = GoodDM()
    # slots.DanmakuContract 只要求 name：good 满足 adapter 一定也满足 slots
    assert isinstance(good, SlotsDM) is True
    assert isinstance(good, DanmakuContract) is True
