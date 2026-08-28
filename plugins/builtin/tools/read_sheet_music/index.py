"""read_sheet_music 工具注册：homr OMR 识谱 → 谱面文件 → 配合 play_score 弹琴。"""

from plugins.builtin.tools.read_sheet_music.sheet import _read_sheet_music


def register(ctx):
    """注册 read_sheet_music：用户给乐谱图片或屏幕上有乐谱时用。"""
    ctx.tools.register(
        name="read_sheet_music",
        description="OMR 识谱：用 homr 光学乐谱识别模型把乐谱图片转成完整可演奏谱面"
                    "（自动识别双手/和弦/休止，输出 score JSON 文件），拿到结果后立即"
                    "调用 play_score(path=谱面文件) 弹出来。path 传乐谱图片路径，"
                    "支持 * 通配符一次读多页（如 e:/sheet/p*.jpg，按文件名排序）；"
                    "不传 path 则截取当前屏幕识别。识别每页约 1 分钟，多页乐谱耐心等待。",
        parameters={
            "path": {
                "type": "string",
                "description": "乐谱图片的本地路径（png/jpg），支持 * 通配符读多页；留空则截取当前屏幕",
            },
            "tempo": {
                "type": "number",
                "description": "弹奏速度 BPM（谱面通常不标速度，默认 88）",
            },
        },
        execute=lambda args: _read_sheet_music(**args),
        timeout=1500.0,  # homr CPU 推理每页约 1 分钟，多页乐谱需要足够时间
        enabled_by="TOOL_READ_SHEET_ENABLED",
    )
