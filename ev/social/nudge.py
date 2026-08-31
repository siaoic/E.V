"""ev.social.nudge — 主动驱动引擎（Neuro-sama 风格）。

主项目已把 Nudge 引擎实现在 ev/llm/proactive/nudge.py（5 种契机 + observe/
check 双入口 + 防刷 + 统计），本模块是其兼容再导出层：
  - ev.social.nudge.NudgeEngine / NudgeEvent / NudgeReason
  - get_engine(**kwargs)   未初始化时按 kwargs 懒创建（对齐参考实现签名）
  - reset_engine()

不复制引擎本体，避免两套契机引擎并存（弹幕埋点共享同一全局单例）。
"""

from __future__ import annotations

from typing import Optional

from ev.llm.proactive.nudge import (  # noqa: F401
    NudgeEngine,
    NudgeEvent,
    NudgeReason,
    ensure_engine,
    get_engine as _get_engine,
    reset_engine as _reset_engine,
)


def get_engine(**kwargs) -> NudgeEngine:
    """获取全局契机引擎；传 kwargs 时按配置校准阈值（未初始化则创建）。"""
    if kwargs:
        return ensure_engine(**kwargs)
    return _get_engine()


def reset_engine() -> None:
    """重置全局实例（供测试）。"""
    _reset_engine()


# 参考 API 兼容别名
get_nudge_engine = get_engine
reset_nudge_engine = reset_engine


async def check(event: Optional[dict] = None) -> Optional[NudgeEvent]:
    """便捷入口：带事件现场检查契机（参考实现 engine.check 的异步语义）。"""
    engine = get_engine()
    if event:
        return engine.observe(
            event.get("type", ""), event.get("payload") or {})
    return engine.check()


__all__ = [
    "NudgeEngine",
    "NudgeEvent",
    "NudgeReason",
    "get_engine",
    "reset_engine",
    "get_nudge_engine",
    "reset_nudge_engine",
    "check",
]
