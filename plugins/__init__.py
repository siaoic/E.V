"""插件系统（Python 同进程 async 运行时，对标 live-2d 插件体系）。

插件框架、插件本体、本地工具统一放在 plugins/：
  - 框架：base.py / context.py / manager.py（本模块导出）
  - 插件本体：plugins/<插件名>/（metadata.json + index.py，见 example/）
  - 本地工具：plugins/tools/（Function Calling 工具包）
启用状态由 plugins/enabled_plugins.json 控制。

用法：
    from plugins import Plugin

    class MyPlugin(Plugin):
        async def on_start(self):
            self.context.log('info', '启动了')
"""

from plugins.base import (
    LLMRequestEvent,
    LLMResponseEvent,
    Plugin,
    UserInputEvent,
)
from plugins.context import PluginContext
from plugins.manager import (
    PluginManager,
    get_default_manager,
    set_default_manager,
)

__all__ = [
    "Plugin",
    "PluginContext",
    "PluginManager",
    "UserInputEvent",
    "LLMRequestEvent",
    "LLMResponseEvent",
    "get_default_manager",
    "set_default_manager",
]
