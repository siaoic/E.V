"""进程内事件总线（发布 / 订阅）：解耦「生产者」与「订阅者」。

用途：
  - 内核关键节点发布事件（用户输入 / AI 回复 / 播报开始结束 / 错误等）；
  - 新功能（字幕、情绪、监控、未来 WebUI 等）只需 subscribe，无需改动
    生产方代码——新增功能接口天然向后兼容。
  - UI（控制中心）已是独立进程，走 QProcess + stdin/stdout 协议通信
    （console.chat 的 CHAT_TAG 通道）；本总线负责内核内部的标准化事件契约。

用法：
    from ev.kernel.bus import bus, EV_AI_REPLY

    async def handler(payload):
        print(payload.text)

    bus.subscribe(EV_AI_REPLY, handler)
    await bus.emit(EV_AI_REPLY, LLMResponse(text="hi"))
"""

from __future__ import annotations

import fnmatch
from collections import defaultdict
from typing import Awaitable, Callable, Dict, List

from ev.utils import console

# ---- 事件名（常量，订阅/发布统一引用，避免手写字符串拼错） ----
EV_USER_INPUT = "user_input"            # 用户 / 观众输入进入内核（payload: InputEvent）
EV_AI_REPLY = "ai_reply"                # AI 产出回复（payload: LLMResponse，流式逐句）
EV_SPEAKING_START = "speaking_start"    # 一次播报开始（payload: SpeakingEvent）
EV_SPEAKING_END = "speaking_end"        # 一次播报结束（payload: SpeakingEvent）
EV_ERROR = "error"                      # 统一错误事件（payload: ErrorEvent）
EV_STATE_CHANGE = "state_change"        # 全局状态机变化（payload: StateChangeEvent）
EV_SESSION_END = "session_end"          # 会话结束（payload: SessionEndEvent）
EV_TOOL_CALL = "tool_call"              # 工具调用开始（payload: dict{id,name,args}）
EV_TOOL_RESULT = "tool_result"          # 工具执行结束（payload: dict{name,content}）

# 订阅者回调签名：async (payload: Any) -> None
Handler = Callable[[object], Awaitable[None]]

# 通配订阅事件名（on_any）：匹配所有事件
_ANY = "*"


def _is_pattern(event: str) -> bool:
    """事件名是否含通配符（* / ?），含则按 fnmatch 模式匹配。"""
    return any(ch in event for ch in "*?")


class EventBus:
    """极简进程内 pub/sub 总线：单线程事件循环内使用，顺序执行订阅者。

    支持通配符订阅：事件名含 * / ? 时按 fnmatch 匹配（如 "speaking_*"），
    或直接用 on_any() 订阅全部事件。
    """

    def __init__(self) -> None:
        self._handlers: Dict[str, List[Handler]] = defaultdict(list)
        self._patterns: Dict[str, List[Handler]] = defaultdict(list)

    def subscribe(self, event: str, handler: Handler) -> None:
        """订阅事件；同一 handler 重复订阅自动去重。"""
        handlers = self._patterns[event] if _is_pattern(event) else self._handlers[event]
        if handler not in handlers:
            handlers.append(handler)

    def unsubscribe(self, event: str, handler: Handler) -> None:
        """取消订阅；未订阅时静默。"""
        try:
            if _is_pattern(event):
                self._patterns[event].remove(handler)
            else:
                self._handlers[event].remove(handler)
        except ValueError:
            pass

    def on(self, event: str, handler: Handler) -> None:
        """订阅事件（subscribe 别名）。"""
        self.subscribe(event, handler)

    def off(self, event: str, handler: Handler) -> None:
        """取消订阅（unsubscribe 别名）。"""
        self.unsubscribe(event, handler)

    def on_any(self, handler: Handler) -> None:
        """订阅全部事件（等价 subscribe("*", handler)）。"""
        self.subscribe(_ANY, handler)

    def subscribers(self) -> Dict[str, int]:
        """返回 {event_name: handler_count}，覆盖精确 + 通配两种。"""
        info: Dict[str, int] = {}
        for event, handlers in self._handlers.items():
            if handlers:
                info[event] = len(handlers)
        for pattern, handlers in self._patterns.items():
            if handlers:
                info[pattern] = info.get(pattern, 0) + len(handlers)
        return info

    async def emit(self, event: str, payload=None) -> None:
        """广播事件：精确订阅优先，再按通配符匹配；同一 handler 只执行一次。

        单个订阅者异常不影响其余。
        """
        handlers = list(self._handlers.get(event, ()))
        for pattern, hlist in self._patterns.items():
            if fnmatch.fnmatchcase(event, pattern):
                handlers.extend(h for h in hlist if h not in handlers)
        for handler in handlers:
            try:
                await handler(payload)
            except Exception as e:
                console.dim(f"[bus] 事件 {event} 订阅者处理出错：{e}")


# 全局单例：模块内直接用 `bus` 发布 / 订阅
bus = EventBus()
