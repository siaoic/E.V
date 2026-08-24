"""ProactiveEngine 子包：对外导出 ProactiveEngine + 模块级常量/helper。"""

from .core import ProactiveEngine
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

__all__ = ["ProactiveEngine"]
