"""知识注入格式化：把 recall 结果拼成注入 system prompt 的段落。

优先级 L0a（curated 卡片）→ L0b（facts）→ L0c/L1（lore），并受
max_total_chars 总长度预算约束（含小节标题，保证注入长度不超预算）；
无匹配内容时返回空串（不实际注入，避免每轮对话都白白追加 Token）。
"""

from __future__ import annotations

# RAG 式注入引导：检索出的资料仅作事实依据，要求模型用自己的话作答，
# 避免照搬知识原文造成生硬复述
_CURATED_HEADER = "### 角色权威设定（回答相关问题时参考以下设定，用自己的话作答，不照搬原文）"
_FACTS_HEADER = "### 关键事实（作为作答依据，用自己的话表达，不照搬原文）"
_LORE_HEADER = "### 背景资料（作答参考，基于官方设定，用自己的话作答，不编造、不臆测）"

# 闸门已判定「知识相关」（剧情意图）但检索无匹配内容时的 fail-closed
# 约束块：不注入任何「知识」，仅约束模型不得编造——无资料可依的话题
# 最容易产生幻觉，明确告知比静默放行更安全（对标 fail-closed 原则）。
FAIL_CLOSED_BLOCK = (
    "### 资料约束\n"
    "关于这个话题，你目前没有可靠的背景资料。若不确定，请如实说明"
    "「这个我还不清楚」，不要编造设定、人物关系或剧情细节。"
)


def format_for_injection(
    recalled: dict,
    *,
    level: int = 1,
    max_total_chars: int = 1200,
    fail_closed: bool = False,
) -> str:
    """按 level 拼接检索结果：level>=1 注入 curated+facts，level>=2 追加 lore。

    返回空串表示无需注入（闸门误判或无匹配内容时的安全回退）。
    fail_closed=True：无匹配内容时返回防编造约束块而非空串，阻止模型
    在无资料话题上自由发挥幻觉。
    """
    if not recalled:
        return FAIL_CLOSED_BLOCK if fail_closed else ""
    sections = []
    budget = max_total_chars

    def _take(text: str, header: str) -> str:
        """把「标题 + 换行 + 正文」当作一个整体放入预算；超出时截断正文。"""
        nonlocal budget
        if budget <= 0:
            return ""
        body = text
        if len(body) > budget - len(header) - 1:
            body = body[: max(0, budget - len(header) - 1)]
        if not body.strip():
            return ""
        used = len(header) + 1 + len(body)
        budget -= used
        return header + "\n" + body

    # L0a：curated 卡片（内容较长，优先注入）
    curated = recalled.get("curated") or []
    if curated:
        block = "\n\n".join(c for c in curated if c.strip())
        section = _take(block, _CURATED_HEADER)
        if section:
            sections.append(section)

    # L0b：facts（短句，优先级次之）
    facts = recalled.get("facts") or []
    if facts and budget > 0:
        block = "\n".join(f"- {a}" for a in facts if a.strip())
        section = _take(block, _FACTS_HEADER)
        if section:
            sections.append(section)

    # L0c/L1：lore（仅剧情意图等全层注入时带上）
    if level >= 2 and budget > 0:
        lore = recalled.get("lore") or []
        if lore:
            block = "\n\n".join(l.strip() for l in lore if l.strip())
            section = _take(block, _LORE_HEADER)
            if section:
                sections.append(section)

    result = "\n\n".join(sections)
    if not result and fail_closed:
        # 闸门判定知识相关但无任何匹配内容：注入防编造约束而非静默放行
        return FAIL_CLOSED_BLOCK
    return result
