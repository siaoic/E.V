"""ProactiveEngine 创建 + 打印（L385-406）。"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ev.kernel.runtime import RuntimeContext


async def setup(runtime: "RuntimeContext") -> None:
    """ProactiveEngine 创建 + 状态打印。"""
    from ev.utils import console
    from ev.llm.proactive import ProactiveEngine

    cfg = runtime.cfg

    # 主动对话引擎
    runtime.proactive = None
    if cfg.PROACTIVE_ENABLED:
        runtime.proactive = ProactiveEngine(
            brain=runtime.brain, tts=runtime.tts, face=runtime.face, sub=runtime.sub, cfg=cfg,
            butler=runtime.butler if cfg.MEMORY_ENABLED else None,
            memory_manager=runtime.mm if cfg.MEMORY_ENABLED else None,
            profanity_filter=runtime.pf,
            profanity_filter_rate=cfg.PROFANITY_FILTER_RATE,
            emotion_actor=(
                runtime.emotion_actor
                if (getattr(runtime, "emotion_actor", None) is not None
                    and cfg.EMOTION_ACTOR_ENABLED)
                else None),
        )
        console.dim(
            f"主动对话已启用：LLM 自主开口（互动/弹幕结束即给机会，"
            f"静默期每 {cfg.RESPONSE_INTERVAL_MIN:.0f}~"
            f"{cfg.RESPONSE_INTERVAL_MAX:.0f}s 随机给一次机会，"
            f"是否开口由主模型自主决定）")
    else:
        console.dim("主动对话未启用（.env 设置 PROACTIVE_ENABLED=true 开启）")


async def teardown(runtime: "RuntimeContext") -> None:
    """Proactive engine 无显式 stop；随进程结束释放。"""
    return None
