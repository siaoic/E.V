"""ev.kernel.components 包：4.x 旧路径各组件 setup/teardown 函数集合。

各组件对外提供：
- async def setup(runtime: RuntimeContext) -> None
- async def teardown(runtime: RuntimeContext) -> None
"""

from . import (  # noqa: F401  导出各组件模块，供骨架 import 取别名
    agent,
    avatar,
    evolution,
    filter as filter_mod,
    io as io_mod,
    llm,
    mcp,
    memory,
    mindcraft,
    plugin,
    proactive,
    tts,
)

__all__ = [
    "agent",
    "avatar",
    "evolution",
    "filter_mod",
    "io_mod",
    "llm",
    "mcp",
    "memory",
    "mindcraft",
    "plugin",
    "proactive",
    "tts",
]
