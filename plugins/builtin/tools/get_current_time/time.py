"""get_current_time 工具：当前日期时间（可直接朗读）。"""

from datetime import datetime


async def _get_current_time() -> dict:
    """当前时间。返回 dict（tool_registry 自动 json.dumps，避免非 JSON 告警）。"""
    now = datetime.now()
    weekdays = ["一", "二", "三", "四", "五", "六", "日"]
    return {
        # readable：可直接朗读的完整句
        "readable": (
            f"现在时间是 {now.year}年{now.month}月{now.day}日 "
            f"星期{weekdays[now.weekday()]} {now.strftime('%H:%M:%S')}"
        ),
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M:%S"),
        "weekday_cn": f"星期{weekdays[now.weekday()]}",
    }
