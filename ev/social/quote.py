"""ev.social.quote — 引用回复(模块 5/5)

检测 @机器人 / 引用上一条 / 叫人设名 / SC / 礼物,触发"认真接茬"模式。

灵感:qq-bridge 的 reserved2 让 AI 通过 qq_get_unread_messages 工具主动看消息,
本方案等价于:看到自己名字 / 被 @ 时,优先级拉满。

设计:
  - 订阅 EV_DANMAKU_RECV,与 deliberation 并行
  - 把检测结果写到 context,deliberation 据此打分
  - 命中"必回"场景时,主动触发 engagement 状态机从 退场/睡眠 唤醒
  - 提供 build_quote_context() 给 prompt 注入
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("ev.social.quote")


@dataclass
class QuoteSignal:
    """一条弹幕的引用触发信号集合。"""
    is_at_me: bool = False           # @机器人 / @人设名
    is_quoted: bool = False          # 引用了我上一条
    mentions_name: bool = False      # 文本中出现了人设名
    is_superchat: bool = False       # 醒目留言
    is_gift: bool = False            # 礼物
    reply_to_msg_id: Optional[str] = None  # B 站 reply_to_message_id
    forced_reply: bool = False       # 上述任一 → True
    matched_name: Optional[str] = None  # 命中的具体人设名(调试用)
    matched_keyword: Optional[str] = None
    
    def __post_init__(self):
        self.forced_reply = any([
            self.is_at_me, self.is_superchat, self.is_gift,
        ])


# ===== 名字检测 =====
def _load_names() -> set[str]:
    """从 SKILL.md 抓人设名(也包括 persona 拟人化参数里 name_aliases)。
    
    缓存以避免每次重读。
    """
    global _name_cache_loaded
    if not _name_cache_loaded:
        _load_names_from_skill()
        _name_cache_loaded = True
    return _name_cache


_name_cache: set[str] = set()
_name_cache_loaded = False


def _load_names_from_skill() -> None:
    global _name_cache
    try:
        from ev.utils import config as ev_config
        skill_path = getattr(ev_config.cfg, "SYSTEM_PROMPT_FILE", "") or ""
        if not skill_path:
            # 尝试默认路径
            from pathlib import Path
            for p in [
                Path("configs/personas/default/SKILL.md"),
                Path("configs/personas/SKILL.md"),
            ]:
                if p.exists():
                    skill_path = str(p)
                    break
        if not skill_path:
            return
        
        from pathlib import Path
        text = Path(skill_path).read_text(encoding="utf-8")
        
        # 抓 "**姓名：** xxx" / "**Name:** xxx" 等
        for m in re.findall(
            r'(?:姓名|Name|昵称|Nickname|名字)[:：]\s*([^\n]+)',
            text, flags=re.IGNORECASE,
        ):
            m = m.strip()
            if m:
                _name_cache.add(m)
                # 拆字/拆英文
                for part in re.split(r'[\s(/（]', m):
                    if len(part) >= 2:
                        _name_cache.add(part)
        
        # 抓拟人化参数里的 name_aliases
        m_aliases = re.search(
            r'name_aliases:\s*\[([^\]]+)\]',
            text, flags=re.IGNORECASE,
        )
        if m_aliases:
            for a in m_aliases.group(1).split(','):
                a = a.strip().strip('"').strip("'")
                if a:
                    _name_cache.add(a)
        
        # 配置项 SOCIAL_QUOTE_EXTRA_NAMES 追加
        extra = getattr(ev_config.cfg, "SOCIAL_QUOTE_EXTRA_NAMES", "") or ""
        for a in extra.split(','):
            a = a.strip()
            if a:
                _name_cache.add(a)
        
        # 也加上"机器人"这个通用名
        _name_cache.update({"机器人", "bot", "Bot", "主播"})
        
        logger.info(f"[quote] loaded names: {_name_cache}")
    except Exception as e:
        logger.warning(f"[quote] load_names failed: {e}")


def add_name(name: str) -> None:
    """手动补充名字(用于人设别名)。"""
    _name_cache.add(name)


# ===== 兴趣 / 黑名单关键词 =====
def _load_keywords() -> tuple[set[str], set[str]]:
    cfg = _get_quote_config()
    interest = {k.strip() for k in (cfg.get("SOCIAL_QUOTE_INTEREST_KEYWORDS", "") or "").split(',') if k.strip()}
    blocked = {k.strip() for k in (cfg.get("SOCIAL_QUOTE_BLOCKED_KEYWORDS", "") or "").split(',') if k.strip()}
    return interest, blocked


def _get_quote_config() -> dict:
    try:
        from ev.utils import config as ev_config
        return {
            "SOCIAL_QUOTE_INTEREST_KEYWORDS": getattr(ev_config.cfg, "SOCIAL_QUOTE_INTEREST_KEYWORDS", ""),
            "SOCIAL_QUOTE_BLOCKED_KEYWORDS": getattr(ev_config.cfg, "SOCIAL_QUOTE_BLOCKED_KEYWORDS", ""),
            "SOCIAL_QUOTE_RECENT_WINDOW_SEC": getattr(ev_config.cfg, "SOCIAL_QUOTE_RECENT_WINDOW_SEC", 300),
        }
    except Exception:
        return {}


# ===== 主检测函数 =====
def detect_quote_signal(
    text: str,
    *,
    user_id: Optional[str] = None,
    reply_to_message_id: Optional[str] = None,
    is_superchat: bool = False,
    is_gift: bool = False,
    last_ai_msg: Optional[dict] = None,
) -> QuoteSignal:
    """检测一条弹幕的引用信号。
    
    Args:
        text: 弹幕文本
        user_id: 发送者(可选,目前不用)
        reply_to_message_id: blivedm 解析的 reply_to_message_id
        is_superchat: 醒目留言
        is_gift: 礼物
        last_ai_msg: 上一条 AI 自己说的话,字典 {text, platform_msg_id, ts}
    
    Returns:
        QuoteSignal
    """
    signal = QuoteSignal(is_superchat=is_superchat, is_gift=is_gift)
    
    if not text:
        return signal
    
    # 惰性加载人设名缓存（参考实现遗漏了这一步，导致 @ 检测永远为空）
    _load_names()
    
    # ===== @机器人检测 =====
    # B 站 @ 格式: "@xxx " 或 " @xxx"
    at_pattern = re.compile(r"@([^\s@,，]+)")
    for m in at_pattern.finditer(text):
        name = m.group(1)
        if name in _name_cache:
            signal.is_at_me = True
            signal.matched_name = name
            break
    
    # ===== 引用上句检测 =====
    if last_ai_msg:
        # 路径 1: reply_to_message_id 命中
        cfg = _get_quote_config()
        window_sec = int(cfg.get("SOCIAL_QUOTE_RECENT_WINDOW_SEC", 300))
        
        is_recent = (time.time() - float(last_ai_msg.get("ts", 0))) < window_sec
        
        if is_recent and reply_to_message_id and last_ai_msg.get("platform_msg_id"):
            if str(reply_to_message_id) == str(last_ai_msg["platform_msg_id"]):
                signal.is_quoted = True
                signal.reply_to_msg_id = reply_to_message_id
        
        # 路径 2: 文本里包含上一句 4+ 字片段
        if not signal.is_quoted and is_recent:
            ai_text = last_ai_msg.get("text", "")
            if ai_text:
                snippet = ai_text[:8]
                if len(snippet) >= 4 and snippet in text:
                    signal.is_quoted = True
    
    # ===== 叫名字检测 =====
    if not signal.is_at_me:
        for name in _name_cache:
            if name and name in text:
                signal.mentions_name = True
                signal.matched_name = name
                break
    
    # 重算 forced_reply（__post_init__ 只在构造时执行，而上面的信号是
    # 构造后赋值的 —— 不重算的话 @机器人 / SC / 礼物永远不会触发必回）
    signal.forced_reply = any([
        signal.is_at_me, signal.is_superchat, signal.is_gift,
    ])
    
    return signal


# ===== Prompt 上下文注入 =====
def build_quote_context(signal: QuoteSignal, last_ai_msg: Optional[dict] = None) -> str:
    """根据 QuoteSignal 生成 prompt 注入片段。
    
    调用方在 build_system_prompt 时调用,得到片段后拼到 system prompt 末尾。
    """
    if not signal:
        return ""
    
    parts = []
    
    if signal.is_quoted and last_ai_msg:
        parts.append(f"## 用户引用了你上一条\n{last_ai_msg.get('text', '')}\n\n请接着这个话茬继续。")
    
    if signal.is_at_me:
        parts.append("## 用户 @ 了你\n请正面回应,不要沉默。")
    
    if signal.mentions_name and not signal.is_at_me:
        parts.append(
            f"## 用户提到了你的人设名({signal.matched_name or '你的名字'})\n"
            f"请用第一人称回应,不要破功。"
        )
    
    if signal.is_superchat:
        parts.append(
            f"## 醒目留言(SC)\n"
            f"请认真回应,语气尊重。"
        )
    
    if signal.is_gift:
        parts.append("## 用户送出了礼物\n请致谢。")
    
    return "\n\n".join(parts)


# ===== 状态机唤醒 =====
async def maybe_wake_engagement(signal: QuoteSignal) -> None:
    """被叫到时,主动从退场/睡眠里唤醒。"""
    if not signal.forced_reply:
        return
    
    try:
        from .engagement import get_state, transition_to, STATE_OBSERVE, STATE_SLEEP, STATE_EXIT
        state = get_state()
        if state in (STATE_SLEEP, STATE_EXIT):
            reason = f"forced_reply:{signal.matched_name or 'unknown'}"
            await transition_to(STATE_OBSERVE, reason=reason)
    except Exception as e:
        logger.debug(f"[quote] maybe_wake_engagement error: {e}")


# ===== AI 上一条消息的全局缓存(供引用检测)=====
_last_ai_msg: dict = {"text": "", "platform_msg_id": None, "ts": 0.0}


def record_ai_msg(text: str, platform_msg_id: Optional[str] = None) -> None:
    """记录 AI 自己刚说的话,供下次引用检测。"""
    global _last_ai_msg
    _last_ai_msg = {
        "text": text,
        "platform_msg_id": platform_msg_id,
        "ts": time.time(),
    }


def get_last_ai_msg() -> dict:
    return _last_ai_msg.copy()
