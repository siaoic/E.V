"""read_sheet_music 工具注册：homr OMR 识谱 → 谱面文件 → 配合 play_score 弹琴。"""

from plugins.builtin.tools.read_sheet_music.sheet import _read_sheet_music


def register(ctx):
    """注册 read_sheet_music：用户给乐谱图片或屏幕上有乐谱时用。"""
    ctx.tools.register(
        name="read_sheet_music",
        description="OMR 识谱：用 homr 光学乐谱识别模型把乐谱图片转成完整可演奏谱面"
                    "（自动识别双手/和弦/休止，输出 score JSON 文件），拿到结果后立即"
                    "调用 play_score(path=谱面文件) 弹出来。path 原样传用户提供的路径或"
                    "文件名（相对路径按工作目录自动解析，禁止自行补目录），"
                    "支持 * 通配符一次读多页（按文件名排序）；"
                    "不传 path 则截取当前屏幕识别。识别每页约 1 分钟，多页乐谱耐心等待。"
                    "识别成功后、调用 play_score 之前，先回复用户一句："
                    "「学会了，这就给你弹琴」；识别失败则如实说明。"
                    "【捷径】若目标文件夹里已有同名 *.score.json（之前识别过的曲子，"
                    "如 data/epiano/sheets/ 下 epiano_search 标记 omr_ready 的），"
                    "跳过识别直接弹：play_score(path=具体某个 .score.json 文件)。"
                    "path 必须是单个真实文件（禁止 * 通配符，服务器按路径直接读文件）；"
                    "多页乐谱对 page-01、page-02… 依次调用 play_score。"
                    "仅当目标确实是乐谱图片时才调用：普通照片/截图/非乐谱图片不要调用，"
                    "直接告诉用户那不是能弹的乐谱；.txt/.mid 等非图片文件禁止传入。",
        parameters={
            "path": {
                "type": "string",
                "description": "乐谱图片的本地路径或文件名（png/jpg），原样传用户给的值，"
                               "支持 * 通配符读多页；留空则截取当前屏幕",
            },
            "tempo": {
                "type": "number",
                "description": "弹奏速度 BPM（可选）。不传则依次采用：MusicXML 标注 → "
                               "图上速度标记视觉识别；都没有才不写速度字段",
            },
        },
        execute=lambda args: _read_sheet_music(**args),
        timeout=1500.0,  # homr CPU 推理每页约 1 分钟，多页乐谱需要足够时间
        enabled_by="TOOL_READ_SHEET_ENABLED",
    )
