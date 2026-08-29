"""everyonepiano_crawler 工具注册：EveryonePiano 钢琴谱搜索/榜单 + 五线谱下载。

两个工具（目录即注册，无需中央注册表）：
  - epiano_search   按浏览次数返回全站榜单或歌名搜索结果（拿到 music_id）
  - epiano_download 下载指定曲谱的五线谱高清 PNG（可选 EOP 文件）
"""

from plugins.builtin.tools.everyonepiano_crawler.epiano import (
    epiano_download,
    epiano_search,
)


def register(ctx):
    """注册 everyonepiano_crawler 的两个工具（enabled_by: TOOL_EPIANO_ENABLED）。"""
    ctx.tools.register(
        name="epiano_search",
        description=(
            "EveryonePiano 钢琴谱搜索/榜单：按浏览次数降序返回曲谱列表。"
            "用户想要钢琴谱/五线谱/谱子/eop，或点歌让你弹（如「给我弹个xxx」）、"
            "或问「什么谱最热门」时先用本工具。"
            "【调用前必须先回复用户一句：我就先瞅两眼学一下，稍等马上给你弹】"
            "（避免用户干等）。keyword 留空 = 全站浏览次数 Top 榜单；填歌名 = 站内搜索"
            "（同一首歌常有多版本：超简单版/简单版/C调版…，已按浏览次数排序，"
            "第 1 个即最热门版本）。返回 items 里带 local 标记 = 这首已下载过"
            "（甚至已识别），无需再下载，直接按 local 信息弹奏。"
            "从 items 挑 music_id 调 epiano_download。"
        ),
        parameters={
            "keyword": {
                "type": "string",
                "description": "歌名或关键词；留空 = 全站浏览次数最多的榜单",
            },
            "limit": {
                "type": "number",
                "description": "最多返回条数（默认 10，最大 30）",
            },
        },
        execute=lambda args: epiano_search(**args),
        timeout=60.0,
        enabled_by="TOOL_EPIANO_ENABLED",
    )

    ctx.tools.register(
        name="epiano_download",
        description=(
            "下载 EveryonePiano 曲谱的五线谱高清 PNG（约 2500×3400，"
            "按歌名存到 data/epiano/sheets/{歌名}/page-NN.png），可选 EOP 文件。"
            "必须先用 epiano_search 拿到 music_id 再调用；已下载过的会秒回现成文件。"
            "下载成功后自动记入琴谱库和长期记忆（下次点同一首歌直接秒弹）。"
            "返回 omr_ready=false 时接着调 read_sheet_music(path=返回的 "
            "rel_folder+'/page-*.png') 识别；omr_ready=true 时直接 play_score。"
            "下载约需数秒到一分钟，调用前先告诉用户「正在把谱子拿回来，稍等」。"
        ),
        parameters={
            "music_id": {
                "type": "number",
                "description": "曲谱 ID（epiano_search 返回的 items[].music_id）",
                "required": True,
            },
            "title": {
                "type": "string",
                "description": "曲名（epiano_search 返回的 items[].title，用作文件夹名）；"
                               "不传会自动从详情页解析",
            },
            "with_eop": {
                "type": "boolean",
                "description": "是否同时下载 EOP 谱面文件（默认 false）",
            },
        },
        execute=lambda args: epiano_download(**args),
        timeout=300.0,
        enabled_by="TOOL_EPIANO_ENABLED",
    )
