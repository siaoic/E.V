"""轻量级性能埋点工具（首字延迟监控）。

用法：
    tracker = PerfTracker("对话轮次")
    tracker.begin("端到端")
    ...
    tracker.end("LLM 首字")
    tracker.end("端到端")
    tracker.print_report()

上下文管理器（同步/异步均可）：
    with tracker.measure("TTS 合成"):
        do_something()
"""

import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import List, Optional

from src.utils import console
from src.utils.console import paint

# 报告着色用的颜色常量（直接复用 console，避免重复定义）
_GRAY = console.GRAY
_CYAN = console.CYAN
_YELLOW = console.YELLOW
_BRIGHT_YELLOW = console.BRIGHT_YELLOW

# 延迟分级阈值（毫秒）：>2s 亮黄警示、>500ms 黄、其余灰
_SLOW_MS = 2000.0
_MEDIUM_MS = 500.0


@dataclass
class _Sample:
    label: str
    start: float
    end: float
    metadata: str = ""


class PerfTracker:
    """线程安全（同一协程内使用），按 label 累加采样。"""

    def __init__(self, name: str = "") -> None:
        self.name = name
        self._samples: List[_Sample] = []

    # ── 手动打点 ──

    def begin(self, label: str) -> None:
        """开始计时（覆盖同名未结束的 begin）。"""
        self._samples.append(_Sample(label=label, start=time.perf_counter(), end=0.0))

    def end(self, label: str, metadata: str = "") -> None:
        """结束最近一个同名 begin 并记录 metadata。"""
        for sample in reversed(self._samples):
            if sample.label == label and sample.end == 0.0:
                sample.end = time.perf_counter()
                sample.metadata = metadata
                return

    # ── 上下文管理器 ──

    @contextmanager
    def measure(self, label: str, metadata: str = ""):
        start = time.perf_counter()
        yield
        self._samples.append(_Sample(label=label, start=start,
                                     end=time.perf_counter(), metadata=metadata))

    # ── 报告 ──

    def get_duration_ms(self, label: str) -> Optional[float]:
        """取最近一个已结束样本的耗时（毫秒），无则 None。"""
        for sample in reversed(self._samples):
            if sample.label == label and sample.end > 0:
                return (sample.end - sample.start) * 1000
        return None

    def report_lines(self) -> List[str]:
        lines: List[str] = []
        if self.name:
            lines.append(f"  {paint('⏱', _GRAY)} {self.name}")
        for sample in self._samples:
            meta = f"  {paint(sample.metadata, _GRAY)}" if sample.metadata else ""
            if sample.end == 0.0:
                # 未结束的样本（如 LLM 报错中断，首字从未到达）→ 不产生负数
                lines.append(f"    {paint(sample.label.ljust(18), _CYAN)}"
                             f"{paint('--', _GRAY)}（未完成）{meta}")
                continue
            duration_ms = (sample.end - sample.start) * 1000
            color = (_BRIGHT_YELLOW if duration_ms > _SLOW_MS
                     else _YELLOW if duration_ms > _MEDIUM_MS
                     else _GRAY)
            lines.append(f"    {paint(sample.label.ljust(18), _CYAN)}"
                         f"{paint(f'{duration_ms:>7.1f} ms', color)}{meta}")
        return lines

    def report(self) -> str:
        return "\n".join(self.report_lines())

    def print_report(self) -> None:
        text = self.report()
        if text:
            print(text)
