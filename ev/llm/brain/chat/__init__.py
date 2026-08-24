"""流式对话子包：对外导出 _ChatMixin（LLMBrain 的 chat 能力 Mixin）。

内部按职责拆分：
- mixin.py       : 对外类 _ChatMixin（chat_stream / _request_final_reply + thin wrapper）
- inner_loop.py  : _chat_stream_inner 主循环（system 组装 + 多轮流式工具调用 + yield）
- inner_tail.py  : 副作用收尾（历史保存/裁剪/落盘/复盘/性能报告，无 yield）
"""

from .mixin import _ChatMixin

__all__ = ["_ChatMixin"]
