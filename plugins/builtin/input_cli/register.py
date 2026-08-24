from __future__ import annotations
import asyncio
import sys
from typing import AsyncIterator, Optional


def register(ctx) -> None:
    cfg = ctx.config
    impl_name = cfg.get("impl_name", "cli")
    prompt = cfg.get("prompt", "[EV] > ")

    from ev.kernel.slots import SlotName
    if ctx.slots is None:
        ctx.log("warn", "ctx.slots 为空，跳过 input 注册")
        return
    impl = CLIInputSource(prompt=prompt, name=impl_name)
    try:
        ctx.slots.register(SlotName.input, impl_name, impl)
    except Exception as e:
        ctx.log("error", f"注册 input 槽位失败: {e}")
        return
    ctx.log("ok", f"已注册 Input: {impl_name}")


class CLIInputSource:
    """InputContract 最小 stub（CLI 行输入）。

    同时满足 slots.py InputContract Protocol（start/stop/result_future）
    和骨架 async iterator 行为。
    真实实现会 loop 异步读 stdin；本骨架只存 running，并返回空迭代器。
    """

    def __init__(self, prompt: str = "[EV] > ", name: str = "cli") -> None:
        self.name = name
        self.prompt = prompt
        self.running: bool = False
        self._result_future: Optional[asyncio.Future] = None

    # ---- InputContract Protocol 方法（实现为 async，骨架用；Protocol 只检查 callable） ----
    async def start(self) -> None:
        """InputContract.start → async 版本（骨架 stub，await 友好）。"""
        self.running = True
        # 创建一个已完成的 Future 作为 stub（骨架不需要真实等待输入）
        try:
            loop = asyncio.get_event_loop()
            self._result_future = loop.create_future()
            self._result_future.set_result(None)
        except Exception:
            self._result_future = None

    async def stop(self) -> None:
        """InputContract.stop → async 版本（骨架 stub，await 友好）。"""
        self.running = False
        if self._result_future is not None and not self._result_future.done():
            self._result_future.cancel()

    def result_future(self) -> asyncio.Future:
        """InputContract.result_future → 返回一个 Future。"""
        if self._result_future is None:
            try:
                loop = asyncio.get_event_loop()
                self._result_future = loop.create_future()
                self._result_future.set_result(None)
            except Exception:
                # 极端兜底：创建一个手动完成的 Future（无 event loop 时不报错）
                class _StubFut:
                    _done = True
                    _result = None
                    def done(self): return True
                    def result(self): return None
                    def cancel(self): return False
                    def cancelled(self): return False
                    def add_done_callback(self, fn): pass
                self._result_future = _StubFut()
        return self._result_future

    # ---- async iterator（骨架 stub 行为，test 会用到） ----
    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        # stub: 骨架版本直接结束（真实实现在 T16 补齐：
        # asyncio.get_event_loop().run_in_executor + input()）
        if not self.running:
            raise StopAsyncIteration
        # 这里仅做一个"结束立即"占位
        self.running = False
        raise StopAsyncIteration
