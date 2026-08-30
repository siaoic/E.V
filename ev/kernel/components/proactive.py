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
        force_note = ("，冷场兜底强制开口"
                      if cfg.PROACTIVE_FORCE_SPEAK else "，纯自主不强推")
        console.dim(
            f"主动对话已启用（Neuro 风格契机驱动）："
            f"契机命中（冷场≥{cfg.PROACTIVE_NUDGE_LONG_SILENCE_SEC:.0f}s/"
            f"未读≥{cfg.PROACTIVE_NUDGE_MANY_UNREAD}条/太久没说≥"
            f"{cfg.PROACTIVE_NUDGE_SILENT_TOO_LONG_SEC:.0f}s/弹幕爆发）才问主模型，"
            f"开口或 [SILENT] 沉默由模型自主决定"
            f"（静默期每 {cfg.RESPONSE_INTERVAL_MIN:.0f}~"
            f"{cfg.RESPONSE_INTERVAL_MAX:.0f}s 检查一次契机{force_note}）")
    else:
        console.dim("主动对话未启用（.env 设置 PROACTIVE_ENABLED=true 开启）")


async def teardown(runtime: "RuntimeContext") -> None:
    """Proactive engine 无显式 stop；随进程结束释放。"""
    return None
