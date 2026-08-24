"""核心处理器（§3.7 拆分）：input / chat / danmaku / mindcraft。"""

from ev.kernel.handlers.base import BaseHandler
from ev.kernel.handlers.input import InputHandler
from ev.kernel.handlers.chat import ChatHandler

__all__ = ["BaseHandler", "InputHandler", "ChatHandler"]
