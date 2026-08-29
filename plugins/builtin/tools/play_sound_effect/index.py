"""play_sound_effect 工具注册（L3-C）：播放音效增强表现力。"""

from plugins.builtin.tools.sfx import _play_sound_effect


def register(ctx):
    """注册 play_sound_effect：音效编号播放，失败正常回应不要反复重试。"""
    ctx.tools.register(
        name="play_sound_effect",
        description="播放音效来增强对话的趣味性和表现力（如惊讶、爆炸、wow）。"
                    "想播音效时可先调用 list_sound_effects 查看可用音效，再按编号播放；"
                    "或直接用编号：01=搞啥情况, 02=突然一惊, 03=巨大爆炸, "
                    "04=钢管掉落, 05=OMG不可思议, 06=震撼管弦乐, 07=wow效果音。,08=警觉"
                    "播放有约 30% 概率失败，失败会返回提示，正常回应即可，不要反复重试。"
                    "若想音效与叙述同步（讲到那一刻响起），不要调用本工具，"
                    "直接在叙述文本对应位置插入标记 {{sfx:编号}}（编号同上），"
                    "系统会在读到该处时自动播放，标记本身不会被念出。",
        parameters={
            "sfx_id": {
                "type": "string",
                "description": "音效编号（01-07），或逗号分隔的多个音效，如 '01,03'",
                "required": True,
            },
            "repeat": {
                "type": "integer",
                "description": "连续播放次数（1-10），默认 1 次",
                "minimum": 1,
                "maximum": 10,
                "default": 1,
            },
        },
        execute=lambda args: _play_sound_effect(**args),
        enabled_by="TOOL_PLAY_SFX_ENABLED",
    )
