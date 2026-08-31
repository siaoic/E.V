"""ev.social.deliberation — 选择性决策(模块 2/5)

每条弹幕进来打分,过线才送 LLM。
灵感:qq-bridge 让 AI 仿真群友时不是每条都回,而是选择性参与。

设计:
  - 纯函数,无副作用,只算分
  - 0~1 分,过线 (>= threshold) → 放行
  - SC / 礼物 / @机器人 → 直接 bypass
  - 不阻塞主流程,失败时返回 1.0(全放行,行为回落到原 E.V)
"""

from __future__ import annotations

import re
import time
import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger("ev.social.deliberation")


# ===== 评分结果 =====
@dataclass
class DeliberationResult:
    score: float
    threshold: float
    action: str  # "pass" / "skip" / "bypass"
    reasons: list  # 加分减分的原因,供调试
    
    @property
    def passed(self) -> bool:
        return self.action in ("pass", "bypass")


# ===== 启发式 =====
_QUESTION_RE = re.compile(r"[?？]")  # 问号
_EMOJI_RE = re.compile(
    r"[\U0001F300-\U0001F9FF"
    r"\U0001FA00-\U0001FAFF"
    r"\u2600-\u27BF"
    r"\u2300-\u23FF"
    r"\u2B00-\u2BFF"
    r"\u2700-\u27BF]+"
)
_GIFT_KEYWORDS = {"赠送", "投喂", "礼物", "小电视", "辣条", "B坷垃", "小心心", "告白气球"}
_SC_KEYWORDS = {"醒目留言", "superchat", "SuperChat"}


def _is_repetitive(text: str, recent: list, window: int = 10) -> bool:
    """是不是复读机。"""
    text = text.strip().lower()
    for prev in recent[-window:]:
        if prev.strip().lower() == text:
            return True
    return False


def _is_emoji_only(text: str) -> bool:
    """纯表情。"""
    stripped = _EMOJI_RE.sub("", text).strip()
    return len(stripped) == 0 and len(text.strip()) > 0


def _has_question(text: str, min_len: int = 4) -> bool:
    """问句(包含问号且长度够)。"""
    return _QUESTION_RE.search(text) is not None and len(text) >= min_len


# ===== 主评分函数 =====
def score(
    text: str,
    *,
    user_id: Optional[str] = None,
    is_gift: bool = False,
    is_superchat: bool = False,
    is_at_me: bool = False,
    is_quoted: bool = False,
    reply_to_ai: bool = False,
    mentions_name: bool = False,
    interest_keywords: Optional[list] = None,
    blocked_keywords: Optional[list] = None,
    frequent_chatters: Optional[set] = None,
    muted_users: Optional[set] = None,
    recent_danmakus: Optional[list] = None,
) -> DeliberationResult:
    """给一条弹幕打分。
    
    全部参数可选;调用方能传多少传多少,缺省时退到保守(略过线)。
    """
    text = (text or "").strip()
    reasons = []
    s = 0.0
    
    if not text:
        return DeliberationResult(0.0, 0.5, "skip", ["empty"])
    
    # 黑名单直接 0
    if muted_users and user_id in muted_users:
        return DeliberationResult(0.0, 0.5, "skip", [f"user_muted:{user_id}"])
    
    if blocked_keywords:
        for kw in blocked_keywords:
            if kw and kw in text:
                return DeliberationResult(0.0, 0.5, "skip", [f"blocked_kw:{kw}"])
    
    # ===== Bypass:这些场景必回 =====
    if is_superchat:
        return DeliberationResult(1.0, 0.0, "bypass", ["superchat"])
    if is_gift:
        return DeliberationResult(0.95, 0.0, "bypass", ["gift"])
    if is_at_me:
        return DeliberationResult(0.9, 0.0, "bypass", ["at_me"])
    
    # ===== 加分项 =====
    if is_quoted or reply_to_ai:
        s += 0.4
        reasons.append("quoted_ai")
    
    if mentions_name:
        s += 0.4
        reasons.append("mentions_name")
    
    if interest_keywords:
        for kw in interest_keywords:
            if kw and kw in text:
                s += 0.3
                reasons.append(f"interest_kw:{kw}")
                break  # 只记首个命中
    
    if _has_question(text):
        s += 0.25
        reasons.append("question")
    
    if frequent_chatters and user_id in frequent_chatters:
        s += 0.15
        reasons.append("frequent_chatter")
    
    if any(kw in text for kw in _GIFT_KEYWORDS):
        s += 0.4
        reasons.append("gift_keyword")
    
    # ===== 减分项 =====
    if len(text) <= 2:
        s -= 0.2
        reasons.append("too_short")
    
    if _is_emoji_only(text):
        s -= 0.4
        reasons.append("emoji_only")
    
    if recent_danmakus and _is_repetitive(text, recent_danmakus):
        s -= 0.3
        reasons.append("repetitive")
    
    if text in ("?", "?", "？", "？", "?", "..."):
        s -= 0.5
        reasons.append("single_punct")
    
    s = max(0.0, min(1.0, s))
    
    return DeliberationResult(score=s, threshold=0.5, action="pending", reasons=reasons)


def should_pass(
    text: str,
    *,
    engagement_state: str = "observe",
    social_level: float = 0.5,
    is_gift: bool = False,
    is_superchat: bool = False,
    is_at_me: bool = False,
    is_quoted: bool = False,
    mentions_name: bool = False,
    **kwargs,
) -> DeliberationResult:
    """高层接口:直接给"过不过"。
    
    threshold = base * (1 - social_level) + 0.2  ← SKILL.md 里的 social_level 介入
    """
    # base threshold 由 engagement 状态决定
    state_modifier = {
        "observe": 0.55,  # 比较挑
        "active": 0.30,   # 几乎都回
        "probe": 0.70,    # 严挑
        "exit": 0.80,     # 几乎不回
        "sleep": 1.0,     # 永不过
    }
    base = state_modifier.get(engagement_state, 0.55)
    
    # 社牛度调节:社牛降低阈值(更愿意回),社恐提高
    threshold = base * (1 - social_level) + 0.2
    threshold = max(0.1, min(0.95, threshold))
    
    result = score(
        text,
        is_gift=is_gift,
        is_superchat=is_superchat,
        is_at_me=is_at_me,
        is_quoted=is_quoted,
        mentions_name=mentions_name,
        **kwargs,
    )
    result.threshold = threshold
    
    # bypass 直接放行
    if result.action == "bypass":
        return result
    
    result.action = "pass" if result.score >= threshold else "skip"
    return result
