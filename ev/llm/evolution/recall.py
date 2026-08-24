"""复盘素材升级：跨会话召回（5.11）。

在内存轮次（turns[-12:]）之外，用 sessiondb FTS5 精确检索「本轮命中的
关键词/人名/梗」的历史消息，追加 [CROSS-SESSION RECALL] 块供复盘参照
（对标 hermes 的 FTS5 session search for cross-session recall）。

- 关键词提取：无第三方分词依赖，中文 2-gram 计数 + 停用字过滤
  （复用 src/llm/utils/bigram.py 的片段思路，这里做计数而非交集）；
- 检索：逐关键词走 sessiondb DISCOVER，按命中关键词数降序取前 N 条，
  并跳过与本轮已出现内容重复的消息（避免召回自己）；
- 负反馈加权：命中历史消息含负向关键词（复用 feedback.is_negative_text）
  时标注「（含负反馈）」，提醒复盘优先关注观众纠正过主播的消息；
- 全程 fail-open：sessiondb 未启用（ENABLE_SESSION_SEARCH=0）或检索失败
  均返回空串，复盘素材与现状完全一致。
"""

from __future__ import annotations

# 停用字（虚词/语气词/高频无义字）：2-gram 两个字都落在集合内时跳过，
# 避免「的了」「么那」类无检索价值片段被提为关键词
_STOP_CHARS = frozenset(
    "的了在是有和就不人都一这那也我你他她它们吗吧啊呢呀嘛哦嗯哈诶"
    "什么怎么这个那个然后一个真的非常很太最都还是就是其实不过因为"
    "所以但是如果可能应该可以没有不要想要喜欢觉得知道吧啦嘛呢"
)

# 单条召回消息最大字符数（超出截断，控制复盘素材体积）
_MAX_HIT_CHARS = 400

# 单轮复盘最多召回条数（对标文档上限 3 条）
_MAX_EVENTS = 3

# 每个关键词最多检索的命中条数（多词合并后取 Top N，不必单词拉满）
_PER_KEYWORD_LIMIT = 3


def _is_han(ch: str) -> bool:
    """是否中文字符（CJK 统一表意文字区）。"""
    return "\u4e00" <= ch <= "\u9fff"


def _extract_keywords(turns: list[dict], top_n: int = 5) -> list[str]:
    """从最近对话轮次提取检索关键词（中文 2-gram 高频 + 停用字过滤）。

    只取用户/弹幕侧文本（role 为 user / danmaku），AI 自述不参与提词，
    保证关键词是「观众在聊什么」而不是主播自己说了什么。
    """
    counts: dict[str, int] = {}
    for turn in turns:
        if not isinstance(turn, dict):
            continue
        role = str(turn.get("role") or "").strip().lower()
        text = str(turn.get("content") or "").strip()
        if role not in ("user", "danmaku") or not text:
            continue
        for i in range(len(text) - 1):
            a, b = text[i], text[i + 1]
            if not (_is_han(a) and _is_han(b)):
                continue
            if a in _STOP_CHARS and b in _STOP_CHARS:
                continue
            gram = a + b
            counts[gram] = counts.get(gram, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)
    return [gram for gram, _ in ranked[:top_n]]


def _recall_hits(db, keywords: list[str], turns: list[dict],
                 max_events: int) -> list[dict]:
    """逐关键词检索历史消息，按命中关键词数降序取前 N 条（去重/去本轮重复）。"""
    # 本轮已出现的文本：sessiondb 已旁路落库，检索可能召回本轮自己，跳过
    seen_texts = {
        str(t.get("content") or "").strip()
        for t in turns if isinstance(t, dict)
    }
    merged: dict[int, dict] = {}
    for kw in keywords:
        res = db.search(query=kw, mode="DISCOVER", limit=_PER_KEYWORD_LIMIT)
        for it in res.get("results") or []:
            mid = it.get("id")
            content = str(it.get("content") or "").strip()
            if mid is None or not content or content in seen_texts:
                continue
            item = merged.get(mid)
            if item is None:
                merged[mid] = {
                    "id": mid, "session_id": it.get("session_id", ""),
                    "role": it.get("role", ""), "content": content,
                    "ts": it.get("ts", ""), "hits": 1,
                }
            else:
                item["hits"] += 1
    ranked = sorted(
        merged.values(),
        key=lambda it: (it["hits"], int(it["id"] or 0)),
        reverse=True,
    )
    return ranked[:max_events]


def cross_session_recall(turns: list[dict], max_events: int = _MAX_EVENTS) -> str:
    """按本轮关键词检索历史会话，返回 [CROSS-SESSION RECALL] 素材块。

    会话库未启用 / 无关键词 / 无命中 / 任何异常 → 返回空串（fail-open），
    复盘素材与现状完全一致。
    """
    if not turns:
        return ""
    try:
        from ev.llm.sessiondb import get_session_db
        db = get_session_db()
    except Exception:
        return ""
    if db is None:
        return ""
    keywords = _extract_keywords(turns)
    if not keywords:
        return ""
    try:
        from .feedback import is_negative_text
        hits = _recall_hits(db, keywords, turns, max_events)
    except Exception:
        return ""
    if not hits:
        return ""
    lines = []
    for it in hits:
        content = it["content"]
        if len(content) > _MAX_HIT_CHARS:
            content = content[:_MAX_HIT_CHARS] + "…"
        mark = "（含负反馈）" if is_negative_text(content) else ""
        ts = (it.get("ts") or "")[:16]
        prefix = f"[{ts}]" if ts else ""
        lines.append(f"- {prefix} {content}{mark}")
    return ("\n\n[CROSS-SESSION RECALL]（本轮关键词命中的历史对话，"
            "供对比观众长期偏好与纠正记录）\n" + "\n".join(lines))
