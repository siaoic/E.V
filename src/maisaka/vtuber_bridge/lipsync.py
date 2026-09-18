# -*- coding: utf-8 -*-
"""口型驱动：TTS 包络 → MouthOpen 曲线 → 30Hz 参数注入。

曲线规则（对齐原 TS 实现）：
- 上行即时：说话瞬间嘴立刻张开（取当前包络与现值的较大者）；
- 下行指数衰减：``τ=40ms``，避免嘴突然闭合的生硬感；
- 静音归零：连续 ``MUTE_AFTER_SEC`` 没有包络输入（或包络归零）时嘴闭合。

包络回调发生在 TTS 播放线程（见 ``player.py`` 的钩子约定），因此
``on_envelope`` 只做带锁的取值更新，所有网络注入都发生在 30Hz 的
asyncio tick 里。
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import threading
import time
from typing import Optional

import numpy as np

from . import state
from .vts_client import VtsClient

_DECAY_TAU_SEC = 0.04  # 下行指数衰减时间常数
_MUTE_AFTER_SEC = 0.25  # 包络静默多久后强制归零
_TICK_HZ_DEFAULT = 30


class LipSyncDriver:
    """订阅一个 TtsPlayer 的包络，按固定节拍向 VTS 注入口型参数。"""

    def __init__(self, *, client: VtsClient, mouth_param: str, mouth_gain: float, mouth_max: float, inject_hz: int, logger) -> None:
        """构建驱动器。

        Args:
            mouth_param: 口型参数名（如 ``MouthOpen``），需在 VTS 白名单内。
            mouth_gain: RMS → 开合度增益；普通说话 RMS 约 0.01~0.15。
            mouth_max: 注入值上限（VTS MouthOpen 默认量程 0..1）。
        """

        self._client = client
        self._mouth_param = mouth_param
        self._mouth_gain = float(mouth_gain)
        self._mouth_max = float(mouth_max)
        self._tick_interval = 1.0 / max(int(inject_hz or _TICK_HZ_DEFAULT), 1)

        self._lock = threading.Lock()
        self._target = 0.0  # 最近一次包络对应的开合度
        self._current = 0.0  # 上一次注入的值（下行衰减的起点）
        self._last_envelope_at = 0.0
        self._task: Optional[asyncio.Task[None]] = None

    # ------------------------------------------------------------------ 生命周期

    def attach(self, player) -> None:
        """订阅 TTS 播放器的包络回调。"""
        player.add_envelope_listener(self.on_envelope)

    def detach(self, player) -> None:
        """取消订阅并让嘴回到闭合。"""
        player.remove_envelope_listener(self.on_envelope)
        self._target = 0.0
        self._current = 0.0

    def start(self) -> None:
        """启动 30Hz 注入节拍；重复调用无副作用。"""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._tick_loop(), name="vtuber-lipsync")

    async def stop(self) -> None:
        """停止节拍并注入一次闭合值。"""
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await self._inject(0.0)

    # ------------------------------------------------------------------ 包络入口（播放线程）

    def on_envelope(self, rms: float, sample_rate: int) -> None:
        """TTS 包络回调：只更新目标值，绝不阻塞（发生在播放线程）。"""

        del sample_rate
        normalized = float(np.clip(rms * self._mouth_gain, 0.0, 1.0))
        with self._lock:
            self._target = normalized
            self._last_envelope_at = time.monotonic()

    # ------------------------------------------------------------------ 30Hz 注入

    async def _tick_loop(self) -> None:
        """固定节拍计算口型曲线并注入。"""
        last_tick = asyncio.get_running_loop().time()
        try:
            while True:
                await asyncio.sleep(self._tick_interval)
                now = asyncio.get_running_loop().time()
                dt = max(now - last_tick, 0.0)
                last_tick = now

                with self._lock:
                    target = self._target
                    current = self._current
                    silent_for = now - self._last_envelope_at

                # 下行 τ=40ms 指数衰减；静音超时强制归零
                if silent_for > _MUTE_AFTER_SEC or (target <= 0.0 and current <= 0.01):
                    value = 0.0
                else:
                    value = max(target, current * math.exp(-dt / _DECAY_TAU_SEC))

                with self._lock:
                    self._current = value

                await self._inject(value)
        except asyncio.CancelledError:
            raise

    async def _inject(self, value: float) -> None:
        """注入一帧口型；未连接时跳过（状态页如实呈现断流）。"""
        wire_value = float(np.clip(value, 0.0, 1.0)) * self._mouth_max
        await self._client.inject_parameters(
            "set",
            [(self._mouth_param, wire_value)],
        )
        state.mark_inject(wire_value)
