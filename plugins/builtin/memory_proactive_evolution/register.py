"""三合一骨架：MemoryLongterm + ProactiveTicker + EvolutionTrace。"""
from __future__ import annotations

import os
from typing import Any, Optional


def register(ctx) -> None:
    cfg = ctx.config
    impl_name_mem = cfg.get("impl_name_memory", "longterm")
    impl_name_pro = cfg.get("impl_name_proactive", "ticker")
    impl_name_evo = cfg.get("impl_name_evolution", "day1-trace")
    top_k: int = int(cfg.get("top_k", 5))
    interval_sec: int = int(cfg.get("interval_sec", 60))
    log_dir: str = cfg.get("log_dir", "./data/evo")

    mem = MemoryLongterm(name=impl_name_mem, top_k=top_k)
    pro = ProactiveTicker(name=impl_name_pro, interval_sec=interval_sec)
    evo = EvolutionTrace(name=impl_name_evo, log_dir=log_dir)

    try:
        from ev.kernel.slots import SlotName
    except Exception as e:
        ctx.log("error", f"无法导入 SlotName: {e}")
        return
    if ctx.slots is None:
        ctx.log("warn", "ctx.slots 为空，跳过 memory/proactive/evolution 注册")
        return
    try:
        ctx.slots.register(SlotName.memory, impl_name_mem, mem)
        ctx.slots.register(SlotName.proactive, impl_name_pro, pro)
        ctx.slots.register(SlotName.evolution, impl_name_evo, evo)
    except Exception as e:
        ctx.log("error", f"注册 memory/proactive/evolution 失败: {e}")
        return
    ctx.log(
        "ok",
        f"已注册 Memory={impl_name_mem} Proactive={impl_name_pro} Evolution={impl_name_evo}",
    )


class MemoryLongterm:
    """MemoryContract 占位（name 字段即可满足）。"""

    def __init__(self, name: str = "longterm", top_k: int = 5) -> None:
        self.name = name
        self.top_k = top_k
        self._store: list[dict[str, Any]] = []

    async def save(self, entry: dict[str, Any]) -> None:
        self._store.append(dict(entry))

    async def query(self, text: str, top_k: Optional[int] = None) -> list[dict[str, Any]]:
        k = top_k or self.top_k
        return list(self._store[-k:])

    def clear(self) -> None:
        self._store.clear()


class ProactiveTicker:
    """ProactiveContract 占位（name 字段即可满足）。"""

    def __init__(self, name: str = "ticker", interval_sec: int = 60) -> None:
        self.name = name
        self.interval_sec = interval_sec
        self.running: bool = False
        self._queue: list[str] = []

    async def start(self) -> None:
        self.running = True

    async def stop(self) -> None:
        self.running = False

    def enqueue(self, text: str) -> None:
        self._queue.append(text)

    def pop(self) -> Optional[str]:
        if self._queue:
            return self._queue.pop(0)
        return None


class EvolutionTrace:
    """EvolutionContract 占位（name 字段即可满足）。"""

    def __init__(self, name: str = "day1-trace", log_dir: str = "./data/evo") -> None:
        self.name = name
        self.log_dir = log_dir
        try:
            os.makedirs(self.log_dir, exist_ok=True)
        except Exception:
            pass
        self._records: list[dict[str, Any]] = []

    async def record(self, entry: dict[str, Any]) -> None:
        self._records.append(dict(entry))

    def recent(self, n: int = 10) -> list[dict[str, Any]]:
        return list(self._records[-n:])
