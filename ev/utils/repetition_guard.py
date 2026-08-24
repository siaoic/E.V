"""复读防护（3.13，端口 Hermes agent/repetition_guard.py）。

模型陷入退化循环时可能把整段输出预算花在重复同一个片段上（Hermes 曾
一次产出 6 万字符的复读响应）。本模块在 LLM 流式产句侧检测"复读主导"
片段，命中即中断输出（宁缺毋怪，与 TTS 怪叫兜底同一原则）。

检测刻意保守：只拦 60+ 字符的逐字重复块、出现 ≥5 次且覆盖片段过半，
普通文本（被截断的句子、重复标题、相似代码行）绝不会误伤。

E.V 场景为流式产句，句子较短，检测作用于"已产出累积文本"（≥400 字符
才启用检查，短回复天然不触发）。
"""

from __future__ import annotations

import math

from ev.utils import config

# 片段至少达到该长度才启用复读检查（短截断里重复 token 很常见，属正常）
MIN_FRAGMENT_LENGTH = 400

# 逐字重复窗口长度：远超日常措辞复用（引文/标题/相似代码）的重复才是信号
_REPEAT_WINDOW = 60

# 一个窗口至少出现这么多次才算复读信号
_MIN_REPEAT_COUNT = 5

# 重复窗口覆盖片段字符的比例下限（过半判"复读主导"）
_DOMINANCE_RATIO = 0.5


def repetition_guard_enabled() -> bool:
    """3.13 复读防护总开关：关闭时 is_repetition_dominated 恒 False。"""
    try:
        return bool(config.cfg.AGENT_REPETITION_GUARD)
    except Exception:
        return False


def is_repetition_dominated(text: str) -> bool:
    """text 是否被逐字重复片段主导（是 → 调用方应中断输出）。

    非字符串 / 空 / 过短输入返回 False（fail-open：拿不准就不拦）。
    """
    if not repetition_guard_enabled():
        return False
    if not isinstance(text, str):
        return False
    n = len(text)
    if n < MIN_FRAGMENT_LENGTH:
        return False

    # 快路径：某一规范化的行重复到覆盖过半（最常见形态：整行复读）
    if _line_repetition_dominated(text, n):
        return True

    # 通用路径：固定长度逐字窗口，逐字符滑动。
    # 一个窗口至少出现 needed 次才能覆盖 DOMINANCE_RATIO 的片段
    needed = max(_MIN_REPEAT_COUNT, math.ceil(n * _DOMINANCE_RATIO / _REPEAT_WINDOW))
    counts: dict[str, int] = {}
    for i in range(n - _REPEAT_WINDOW + 1):
        key = text[i:i + _REPEAT_WINDOW]
        c = counts.get(key, 0) + 1
        if c >= needed:
            return True
        counts[key] = c
    return False


def _line_repetition_dominated(text: str, n: int) -> bool:
    """某条规范化行重复出现且覆盖片段过半则判复读。"""
    counts: dict[str, int] = {}
    for line in text.splitlines():
        norm = line.strip()
        if not norm:
            continue
        counts[norm] = counts.get(norm, 0) + 1
    for line, c in counts.items():
        if c >= _MIN_REPEAT_COUNT and c * len(line) >= n * _DOMINANCE_RATIO:
            return True
    return False
