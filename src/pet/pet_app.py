"""桌宠模式入口（RUN_MODE=pet）：PySide6 渲染 + asyncio 泵桥 + live2d 渲染。

本环境实测：qasync（QEventLoop）与 live2d.v3 的 C++ 扩展（_v3cpp.pyd）共存
时，qasync 的 asyncio 定时器全部失效（Qt 原生 QTimer 正常）——因此这里不用
qasync，改用最朴素的「Qt 定时器泵」桥接 asyncio：

- Qt 事件循环（app.exec）驱动渲染 / 输入 / 气泡等全部 Qt 逻辑；
- 一个 5ms 的 QTimer 每拍调用 `loop.call_soon(loop.stop); loop.run_forever()`：
  BaseEventLoop._run_once 在 _ready 非空时 select(0) 非阻塞，只处理就绪回调
  与到期定时器，绝不阻塞 Qt；
- 主协程 main()（LLM / TTS / 主动对话 / 字幕全部复用同一套代码）与桌宠渲染
  窗口跑在同一个线程，PetFaceDriver 每帧 set_parameters 与 Update/Draw 天然
  互斥，无需加锁；
- asyncio 用 Windows 默认 ProactorEventLoop（asyncio.new_event_loop），保留
  asyncio 子进程能力（MCP stdio 传输）与 aiohttp 网络栈。
"""

import asyncio
import sys
import time

import live2d.v3 as live2d

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from src.pet.widget import PetWidget, BubbleSub

# Qt 泵拍率（毫秒）：同时驱动 asyncio 就绪回调与 Qt 渲染，5ms 对输入/网络足够灵敏
_PUMP_MS = 5

_APP: "QApplication | None" = None
_LOOP: "asyncio.AbstractEventLoop | None" = None
_MAIN_TASK: "asyncio.Task | None" = None  # main() 协程任务：窗口关闭时取消以退出


def _quit_pet() -> None:
    """取消主协程（widget closeEvent 调用）。

    桌面宠物窗口被关闭（Esc / Q / 系统关闭）后，任务取消 → done 回调 →
    app.quit() → app.exec() 返回 → run_pet_app 清理并退出进程。
    """
    global _MAIN_TASK
    if _MAIN_TASK is not None and not _MAIN_TASK.done():
        _MAIN_TASK.cancel()


def _pump() -> None:
    """asyncio 非阻塞步进：处理所有就绪回调 + 到期定时器后立即返回。

    call_soon(loop.stop) 保证 _ready 非空 → BaseEventLoop._run_once 走
    select(0) 分支（GetQueuedCompletionStatus timeout=0），绝不在 IOCP 上
    阻塞——Qt 渲染不会因网络等待而冻结。
    """
    loop = _LOOP
    if loop is None or loop.is_closed():
        return
    try:
        loop.call_soon(loop.stop)
        loop.run_forever()
    except Exception:
        pass


def run_pet_app(coro) -> None:
    """桌宠模式入口：创建 Qt 应用 + asyncio 事件循环，运行主协程。

    与 PySide6 参考 example 一致：live2d.init 必须在 QApplication 之前，
    dispose 在事件循环退出后执行。窗口（PetWidget）由 main() 协程内部
    构造并 show()，Qt 事件循环已运行，GL 上下文就绪后自动加载模型。
    """
    global _APP, _LOOP, _MAIN_TASK
    live2d.init()
    app = QApplication(sys.argv)
    # Windows 默认 ProactorEventLoop：支持 asyncio 子进程（MCP stdio）与 aiohttp
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    _APP, _LOOP = app, loop
    _MAIN_TASK = loop.create_task(coro)
    _MAIN_TASK.add_done_callback(lambda _t: app.quit())

    timer = QTimer()
    timer.setInterval(_PUMP_MS)
    timer.timeout.connect(_pump)
    timer.start()

    task_exc = None
    try:
        app.exec()
    finally:
        timer.stop()
        # 取消主协程与全部后台任务（如 main() 里的 _watch_pet_model_change），
        # 避免 loop 关闭时抛「Task was destroyed but it is pending」噪音
        if _MAIN_TASK is not None and not _MAIN_TASK.done():
            _MAIN_TASK.cancel()
        for t in list(asyncio.all_tasks(loop)):
            if t is _MAIN_TASK:
                continue
            if not t.done():
                t.cancel()
        # 非阻塞泵若干次，给取消传播 / 协程清理机会（不 run_until_complete：
        # 空调度时 select(None) 会永久阻塞 IOCP）
        for _ in range(50):
            pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
            if not pending:
                break
            _pump()
            time.sleep(0.005)
        # 取出未取消任务的异常，清理后重抛（保证错误可见、进程退出码非 0）
        if _MAIN_TASK is not None and _MAIN_TASK.done() and not _MAIN_TASK.cancelled():
            try:
                task_exc = _MAIN_TASK.exception()
            except Exception:
                task_exc = None
        _MAIN_TASK = None
        try:
            loop.close()
        except Exception:
            pass
        try:
            asyncio.set_event_loop(None)
        except Exception:
            pass
        live2d.dispose()
    if task_exc is not None:
        raise task_exc


__all__ = ["PetWidget", "BubbleSub", "run_pet_app"]
