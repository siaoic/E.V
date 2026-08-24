"""TTSEngine 创建 + init_tts_async 后台任务 + evict_tts_cache 清理（L319-333）。"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ev.kernel.runtime import RuntimeContext


async def setup(runtime: "RuntimeContext") -> None:
    """TTSEngine 创建 + init_tts_async + 磁盘缓存清理后台任务。"""
    from ev.tts.engine import TTSEngine

    cfg = runtime.cfg

    # TTS 引擎：后台并行加载（本地模型 + 参考音频预编码约 20s），
    # 不阻塞 LLM / 记忆等后续初始化，启动即开始加载；
    # 未就绪时 speak/drain/stop 由引擎内部守卫静默处理，就绪后自动可用
    runtime.tts = None
    if cfg.GPTSOVITS_REF_AUDIO:
        runtime.tts = TTSEngine()
        asyncio.create_task(runtime.init_tts_async())

    # 磁盘音频缓存清理：与 TTS 服务连接状态解耦，启动即后台执行
    # （引擎连接成功路径也有一次清理，幂等，覆盖「服务晚于主程序启动」场景）
    try:
        from ev.tts.engine import evict_tts_cache
        asyncio.create_task(asyncio.to_thread(evict_tts_cache))
    except Exception:
        pass


async def teardown(runtime: "RuntimeContext") -> None:
    """原 teardown 中 TTS drain/stop 段。"""
    import asyncio

    if runtime.tts is not None:
        try:
            # 停止时先打断播放：drain 立即返回。否则若队列里还有
            # 音频，drain 会等它播完（最多 15s），停止明显变慢。
            runtime.tts.interrupt()
            await asyncio.wait_for(runtime.tts.drain(), timeout=5)
            await asyncio.wait_for(runtime.tts.stop(), timeout=15)
        except Exception:
            pass
