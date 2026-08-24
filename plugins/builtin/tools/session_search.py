"""session_search 工具（对标 Hermes tools/session_search_tool.py 精简落地）。

3.7：会话级精确检索，零 LLM 成本，供 LLM 显式调用回忆历史对话（区别于
memU 的隐式语义向量召回——对精确词/人名/梗，子串命中远比向量近邻可靠）。

四模式（mode 参数）：
- DISCOVER：按关键词搜历史消息（trigram FTS + LIKE 子串兜底，中文 2 字词可命中）
- READ：按消息 id 读单条
- SCROLL：按会话时间线翻页（session_id + before_ts）
- BROWSE：列出指定会话最近消息

返回 OpenAI 工具约定的 JSON 字符串（{"results": [...]} 或 {"error": ...}）；
开关 ENABLE_SESSION_SEARCH=0 时返回明确错误提示（工具本身不暴露给 LLM）。
"""

from __future__ import annotations

import json

_MODES = {"DISCOVER", "READ", "SCROLL", "BROWSE"}
"""支持的模式集合（未知模式回退 DISCOVER）"""


async def _session_search(query: str = "", mode: str = "DISCOVER",
                          session_id: str = "", limit: int = 10,
                          before_ts: str = "", msg_id: int = 0) -> str:
    """按模式检索历史会话，返回 JSON 字符串。"""
    from ev.llm.sessiondb import get_session_db
    db = get_session_db()
    if db is None:
        return json.dumps({
            "error": "会话搜索未启用（ENABLE_SESSION_SEARCH=false），无法检索历史对话。"
        }, ensure_ascii=False)
    mode = (mode or "DISCOVER").strip().upper()
    if mode not in _MODES:
        mode = "DISCOVER"
    result = db.search(
        query=str(query or "").strip(),
        mode=mode,
        session_id=str(session_id or "").strip(),
        limit=int(limit or 10),
        before_ts=str(before_ts or "").strip(),
        msg_id=int(msg_id or 0),
    )
    return json.dumps(result, ensure_ascii=False)
