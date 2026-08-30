"""ProactiveEngine 子包：对外导出 ProactiveEngine + Nudge 契机引擎 + helper。

主动对话架构（Neuro-sama 风格，参考 新建文件夹/EV-Anthropomorphic）：
  - nudge.py     Nudge 契机引擎：事件驱动契机（冷场/未读/氛围/沉默/爆发），
                 命中才问 LLM，接受/拒绝统计供自进化；
  - core.py      ProactiveEngine：契机门控心跳 + request_speak 被动响应 + 决策；
  - executor.py  队列/去重/播报 worker + heartbeat 实现；
  - policies.py  话题种子加载/挑选/冷却/心跳时机。
"""

from .core import ProactiveEngine
from .nudge import (
    NudgeEngine,
    NudgeEvent,
    NudgeReason,
    get_engine,
    ensure_engine,
    reset_engine,
    observe as nudge_observe,
)
from .policies import (
    _SILENT_MARKERS,
    _TOPIC_WEIGHTS,
    _ACTIVE_TOPIC_TIMEOUT,
    _FORCE_SPEAK_QUIET,
    _load_topic_seeds,
    _resolve_topics_path,
    _TOPICS_PATH,
    _pick_topic,
)
from .executor import _speak_item, _worker, _enqueue

__all__ = [
    "ProactiveEngine",
    "NudgeEngine",
    "NudgeEvent",
    "NudgeReason",
    "get_engine",
    "ensure_engine",
    "reset_engine",
    "nudge_observe",
]
