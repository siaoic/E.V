"""list_sound_effects 工具注册（L3-C）：列出音效库全部可用音效。"""

from plugins.builtin.tools.sfx import _list_sound_effects


def register(ctx):
    """注册 list_sound_effects：播放前不确定可选音效时用。"""
    ctx.tools.register(
        name="list_sound_effects",
        description="列出音效库中所有可用的音效编号与含义。"
                    "需要播放音效但不确定有哪些可选时调用。",
        parameters={},
        execute=lambda args: _list_sound_effects(),
        enabled_by="TOOL_PLAY_SFX_ENABLED",
    )
