"""get_current_time 工具：当前日期时间（可直接朗读）。"""

from datetime import datetime


async def _get_current_time() -> str:
    """当前时间（可直接朗读）。"""
    now = datetime.now()
    weekdays = ["一", "二", "三", "四", "五", "六", "日"]
    return (
        f"现在时间是 {now.year}年{now.month}月{now.day}日 星期{weekdays[now.weekday()]} "
        f"{now.strftime('%H:%M:%S')}"
    )
