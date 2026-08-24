"""Kernel 顶层编排 + JobScheduler 定时调度器。

Kernel(slots/event_bus/session_log/plugin_manager + boot/shutdown 生命周期)
JobScheduler(every(interval).do(async_func), cancel_all)
"""
from __future__ import annotations
import asyncio
import os
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from ev.kernel.bus import EventBus, bus
from ev.kernel.slots import SlotRegistry, SlotName
from ev.kernel.profile import Profile
from ev.kernel.session_log import SessionLog

# TurnLease: 不是 5 个丢失文件之一，是 src/core/turn_lease.py 正常搬迁到 ev/kernel/turn_lease.py
# (tracked 所以 git checkout 还原 → v2 正常复制改写)
try:
    from ev.kernel.turn_lease import TurnLease
except Exception:
    class TurnLease:  # type: ignore[no-redef]
        def __init__(self, *a, **kw): pass

# PluginManager: plugins.manager 包独立（题目不在 Day 6b 范围），不存在就用空壳兜底
try:
    from plugins.manager import PluginManager  # type: ignore
except Exception:
    class PluginManager:  # type: ignore[no-redef]
        def __init__(self, *a, **kw): self.enabled = []
        async def boot(self, *a, **kw): return None
        async def shutdown(self): return None


# ================================================================
# JobScheduler
# ================================================================
@dataclass
class _Job:
    interval: float
    task: Callable[[], Awaitable[None]]
    cancel_after: Optional[int] = None
    _run_count: int = 0
    _next_ts: float = 0.0
    _cancelled: bool = False


class JobScheduler:
    def __init__(self) -> None:
        self._jobs: list[_Job] = []
        self._stop = asyncio.Event()
        self._task: Optional[asyncio.Task] = None

    class _Builder:
        def __init__(self, sched: "JobScheduler", interval: float):
            self._sched = sched
            self._interval = interval
            self._cancel_after: Optional[int] = None

        def cancel_after(self, n: int) -> "JobScheduler._Builder":
            """执行 n 次后自动取消。"""
            self._cancel_after = n
            return self

        def do(self, task: Callable[[], Awaitable[None]]) -> _Job:
            job = _Job(
                interval=self._interval,
                task=task,
                cancel_after=self._cancel_after,
                _next_ts=time.monotonic() + self._interval,
            )
            self._sched._jobs.append(job)
            # 懒启动：第一次 do() 即启动后台 run_forever task（TR 4.3 不需要手动 run）
            self._sched._ensure_running()
            return job

    def _ensure_running(self) -> None:
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(self.run_forever())

    def every(self, interval_seconds: float) -> _Builder:
        return JobScheduler._Builder(self, interval_seconds)

    async def run_forever(self) -> None:
        tick = 0.01  # 更细的 tick，保证 0.05s 间隔不误判
        while not self._stop.is_set():
            now = time.monotonic()
            for job in list(self._jobs):
                if job._cancelled:
                    if job in self._jobs:
                        self._jobs.remove(job)
                    continue
                # —— 关键修复：允许一次循环内把已经超时多次的 job 补跑多次，
                # 否则 tick=0.05 时，0.14 睡眠后可能只跑 1 次，达不到 2 次阈值。
                while (not job._cancelled
                       and not self._stop.is_set()
                       and job._next_ts <= now):
                    try:
                        await job.task()
                    except Exception:
                        pass
                    job._run_count += 1
                    job._next_ts += job.interval
                    if (job.cancel_after is not None
                            and job._run_count >= job.cancel_after):
                        job._cancelled = True
                        if job in self._jobs:
                            self._jobs.remove(job)
                        break
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=tick)
            except (asyncio.TimeoutError, TimeoutError):
                pass

    def cancel_all(self) -> None:
        self._stop.set()
        self._jobs.clear()
        if self._task is not None and not self._task.done():
            # 允许调用方继续使用 scheduler，但这里取消现有 task
            self._task.cancel()
        self._task = None


# ================================================================
# Kernel
# ================================================================
class Kernel:
    def __init__(
        self,
        profile_data: dict | Profile,
        builtins_root: Optional[str] = None,
        data_root: Optional[str] = None,
    ) -> None:
        self._profile = (
            profile_data if isinstance(profile_data, Profile)
            else Profile(profile_data)
        )
        # 优先级：显式 builtins_root > profile.resolve() 的 builtins_root 参数
        self._builtins_root: Optional[str] = builtins_root
        try:
            self._profile.resolve(builtins_root=(builtins_root or data_root or "."))
        except Exception:
            pass
        self._data_root = data_root or os.path.abspath(".")

        self.slots: SlotRegistry = SlotRegistry()
        self.event_bus: EventBus = bus
        self.session_log: Optional[SessionLog] = None
        self.plugin_manager: Optional[PluginManager] = None
        self.turn_lease: TurnLease = TurnLease()

        self._booted = False
        self._jobs: Optional[JobScheduler] = None
        self._bg_tasks: set[asyncio.Task] = set()
        self._resolved_cache: Optional[dict] = None

    def attach_plugin_manager(self, pm) -> None:
        """显式注入 PluginManager（TR 14/PluginManager 自身构造时可能依赖 kernel.profile，
        外部创建后挂到 kernel 上，boot 中跳过内部实例化）。"""
        self.plugin_manager = pm

    async def boot(self) -> None:
        if self._booted:
            return
        self.session_log = SessionLog(self._data_root)
        if self.plugin_manager is None:
            # 默认：TR 4.x 形式的最小参数 PluginManager（插件可通过 pm.load 加载）
            try:
                self.plugin_manager = PluginManager(
                    app=None,
                    kernel=self,
                )
            except TypeError:
                self.plugin_manager = PluginManager()
        merged: dict = {}
        try:
            merged = self._profile.resolve(
                builtins_root=(self._builtins_root or self._data_root),
            )
        except Exception:
            merged = {}
        # 若 profile 提供了 plugins.builtin 且 plugin_manager 支持 load_all() 风格，
        # 则走 TR 11：boot(builtin_plugins=...)；否则调用 PluginManager 自带 boot
        try:
            if hasattr(self.plugin_manager, "load_all"):
                await self.plugin_manager.load_all()
            else:
                await self.plugin_manager.boot(
                    builtin_plugins=merged.get("plugins", {}).get("builtin", []),
                    plugin_config=merged.get("plugin_config", {}),
                    kernel=self,
                )
        except Exception:
            pass
        # slots 激活：按 profile.slots 里 impl_name 激活（TR 11 demo.yaml 要求
        # slots.model == "echo-default"，SlotRegistry 需要 activate 后 get() 才返回）
        slots_bind = merged.get("slots") or {}
        for key, impl_name in slots_bind.items():
            try:
                slot = key if isinstance(key, SlotName) else SlotName(key)
            except Exception:
                continue
            reg = self.slots
            if impl_name in reg.get_impl_names(slot):
                try:
                    reg.activate(slot, impl_name)
                except Exception:
                    pass
        self._jobs = JobScheduler()
        self._booted = True

    async def shutdown(self) -> None:
        if not self._booted:
            return
        if self._jobs is not None:
            self._jobs.cancel_all()
            self._jobs = None
        if self.plugin_manager is not None:
            try:
                await self.plugin_manager.shutdown()
            except Exception:
                pass
        self.slots.close_all()
        if self.session_log is not None:
            self.session_log.close()
            self.session_log = None
        for t in list(self._bg_tasks):
            if not t.done():
                t.cancel()
        self._bg_tasks.clear()
        self._booted = False

    @property
    def profile(self):
        """对外统一 dict 接口（兼容 TR 11 / PluginManager.load_all / 脚本读取）。

        若调用方需要 Profile 对象，用 `kernel._profile`（内部）或暴露新 API
        `profile_obj`。此处按测试约定返回 resolved dict。
        """
        if self._resolved_cache is None:
            try:
                self._resolved_cache = self._profile.resolve(
                    builtins_root=(self._builtins_root or self._data_root),
                )
            except Exception:
                self._resolved_cache = self._profile.raw
        return self._resolved_cache

    @property
    def profile_obj(self) -> Profile:
        return self._profile

    @property
    def data_root(self) -> str:
        return self._data_root

    @property
    def is_booted(self) -> bool:
        return self._booted


__all__ = ["Kernel", "JobScheduler"]
