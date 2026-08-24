"""兼容 shim：旧路径 ev.llm.llm_brain → 真实实现搬迁到 ev.llm.brain.*。

外部代码（stream.py / runtime.py）直接 `from ev.llm.llm_brain import LLMBrain`，
这里用 re-export 保持路径不变。真实实现位于：
  - ev/llm/brain/core.py        → LLMBrain 主类（本体 + _InjectionMixin + _SummaryMixin）
  - ev/llm/brain/chat/*.py      → 流式对话循环（多轮工具调用 + 按句切段 yield）
  - ev/llm/brain/curator.py     → L4 会后秘书复盘（低频后台）
"""
from ev.llm.brain import LLMBrain  # noqa: F401

# 保留原模块对外导出的模块级常量，供上游（如原 src/llm/llm_brain.py 引用者）使用。
# 注意：瘦身拆分后常量已就近挪到对应子模块，这里按需 re-export 显式声明过的常用项。
try:
    from ev.llm.brain.core import (  # noqa: F401
        _MEMORY_RECALL_TIMEOUT,
        _FIRST_SEGMENT_MIN_CHARS,
        _PAUSE_SEGMENT_MIN_CHARS,
        _MAX_SEGMENT_CHARS,
    )
except Exception:
    pass

__all__ = ["LLMBrain"]
