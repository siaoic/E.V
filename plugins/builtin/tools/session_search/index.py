"""session_search 工具注册（L3-C）：精确检索历史会话消息。"""

from plugins.builtin.tools.session_search.session_search import _session_search


def register(ctx):
    """注册 session_search：默认关闭（ENABLE_SESSION_SEARCH），开启后暴露。"""
    ctx.tools.register(
        name="session_search",
        description="精确检索历史会话消息（区别于记忆的模糊语义召回）。"
                    "当需要回忆「之前聊过什么/谁说过什么/某句话原话」这类"
                    "精确词、人名、梗时调用，返回匹配的历史消息 JSON。"
                    "mode：DISCOVER 按关键词搜索（默认）；READ 按消息 id 读单条；"
                    "SCROLL 按会话时间线翻页（配 session_id/before_ts）；"
                    "BROWSE 列出指定会话最近消息。",
        parameters={
            "query": {
                "type": "string",
                "description": "搜索关键词（DISCOVER 必填；中文短词如人名、梗均可命中）",
            },
            "mode": {
                "type": "string",
                "enum": ["DISCOVER", "READ", "SCROLL", "BROWSE"],
                "description": "检索模式，默认 DISCOVER",
            },
            "session_id": {
                "type": "string",
                "description": "会话标识（SCROLL/BROWSE 用）",
            },
            "limit": {
                "type": "integer",
                "description": "返回条数上限（1-50，默认 10）",
            },
            "before_ts": {
                "type": "string",
                "description": "时间线翻页锚点（SCROLL：只取早于此时间戳的消息）",
            },
            "msg_id": {
                "type": "integer",
                "description": "消息 id（READ 模式用）",
            },
        },
        execute=lambda args: _session_search(**args),
        enabled_by="ENABLE_SESSION_SEARCH",
    )
