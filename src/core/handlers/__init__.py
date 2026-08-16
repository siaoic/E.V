"""核心处理器（§3.7 拆分）：input / chat / danmaku / mindcraft。"""

from src.core.handlers.base import BaseHandler
from src.core.handlers.input import InputHandler
from src.core.handlers.chat import ChatHandler

__all__ = ["BaseHandler", "InputHandler", "ChatHandler"]
