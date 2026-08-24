"""write_diary 工具注册（L3-C）：基于当天对话写日记并落盘。"""

from plugins.builtin.tools.diary import _write_diary


def register(ctx):
    """注册 write_diary：用户要求写日记/记日记时调用。"""
    ctx.tools.register(
        name="write_diary",
        description="基于当天的对话写一篇日记并保存（data/diary/YYYY-MM-DD.md，"
                    "当天已有日记会自动合并重写不丢内容）。当用户要求"
                    "「写日记」「记日记」「写今天的日记」「记录今天」时调用。",
        parameters={},
        execute=lambda args: _write_diary(),
        enabled_by="TOOL_WRITE_DIARY_ENABLED",
    )
