"""World 框架基座：事件驱动多 World 架构的主程序侧实现。

- 各具体世界（Minecraft / PvZ / Bilibili / ASR …）由**插件**实现；
- 本包提供与具体世界无关的公共骨架：注册表、状态缓存、事件总线、管理器、
  以及给 Planner 用的通用工具。
- 插件通过主程序能力 ``world.register`` / ``world.unregister`` /
  ``world.event`` / ``world.state`` 与基座交互；基座通过插件运行时的
  ``invoke_api`` 回呼世界的 ``world_poll`` / ``world_observe`` API。
"""

from .manager import WorldManager, get_world_manager
from .tool_provider import WorldToolProvider
from .types import WorldDescriptor, WorldEvent, WorldEventTrigger

__all__ = [
    "WorldDescriptor",
    "WorldEvent",
    "WorldEventTrigger",
    "WorldManager",
    "WorldToolProvider",
    "get_world_manager",
]
