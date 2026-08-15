"""记忆系统包：基于 memU 引擎（memu/src）的 vtuber 记忆层。

对外暴露 memory 子模块（MemoryManager + 模块级 API），
底层由 memU 的 MemoryService 提供存储与向量检索。
"""

from tools.memory import memory

__all__ = ["memory"]
