"""get_current_time 工具注册（L3-C）：当前日期时间。"""

from plugins.builtin.tools.time import _get_current_time


def register(ctx):
    """注册 get_current_time：只有在想知道时间时才用。"""
    ctx.tools.register(
        name="get_current_time",
        description="用于获取当前日期和时间（含星期）。只有在想知道时间是什么时候才用。",
        parameters={},
        execute=lambda args: _get_current_time(),
        enabled_by="TOOL_GET_CURRENT_TIME_ENABLED",
    )
