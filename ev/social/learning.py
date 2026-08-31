"""ev.social.learning — 社会学习(模块 4/5)

从弹幕区学新词 / 梗,沉淀到 slang_lexicon.jsonl,下次 prompt 自动注入。

灵感:qq-bridge 用 web_search MCP 查新词,本方案升级为「被动监听 + 后台沉淀 + 主动召回」。

设计:
  - 被动监听:每条弹幕进来,异步跑启发式,候选词入队
  - 后台沉淀:5min 一次批量,频次 ≥ 3 的词喂给 LLM 判定
  - 主动召回:每轮 prompt 注入 Top-K 词,与当前情绪匹配
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import logging
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger("ev.social.learning")


@dataclass
class LexiconEntry:
    word: str
    meaning: str = ""
    tone: str = "neutral"  # 夸赞 / 嘲讽 / 中性 / 夸张 / 自嘲
    first_seen_ts: float = 0.0
    use_count: int = 0
    last_used_ts: float = 0.0
    status: str = "accepted"  # accepted / skipped / pending
    
    def to_dict(self) -> dict:
        return asdict(self)
    
    @classmethod
    def from_dict(cls, d: dict) -> "LexiconEntry":
        return cls(**{k: d[k] for k in d if k in cls.__dataclass_fields__})


# ===== 候选词检测启发式 =====
_WORD_RE = re.compile(r"[\u4e00-\u9fff]{2,8}|[A-Za-z]{2,12}")  # 中文 2~8 字 / 英文 2~12 字
_COMMON_STOPWORDS = {
    "哈哈", "哈哈哈", "呵呵", "嘿嘿", "嘻嘻",
    "好的", "可以", "不行", "主播", "机器人", "风花", "fuka", "Fuka",
    "666", "233", "啊啊", "嗯嗯", "哦哦",
    "http", "https", "www", "com", "cn",
    "啊这", "草草", "牛逼", "牛牛",
}


def _is_candidate_word(text: str) -> bool:
    """启发式:这段文本里的词,值不值得纳入候选。"""
    if not text:
        return False
    if text in _COMMON_STOPWORDS:
        return False
    if len(text) > 12:
        return False
    if any(c in text for c in "@#￥%&*<>:：/\\"):
        return False
    return True


# ===== 词库操作 =====
_LEXICON_PATH: Optional[Path] = None


def _resolve_lexicon_path() -> Path:
    global _LEXICON_PATH
    if _LEXICON_PATH:
        return _LEXICON_PATH
    try:
        from ev.utils import config as _cfg
        p = Path(_cfg.cfg.DATA_ROOT) / "social" / "slang_lexicon.jsonl"
    except Exception:
        p = Path("data/social/slang_lexicon.jsonl")
    p.parent.mkdir(parents=True, exist_ok=True)
    _LEXICON_PATH = p
    return p


def load_lexicon() -> list[LexiconEntry]:
    """从磁盘加载全部词条。"""
    p = _resolve_lexicon_path()
    if not p.exists():
        return []
    entries = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
            entries.append(LexiconEntry.from_dict(d))
        except Exception:
            continue
    return entries


def save_lexicon(entries: list[LexiconEntry]) -> None:
    """覆盖写回。"""
    p = _resolve_lexicon_path()
    with p.open("w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e.to_dict(), ensure_ascii=False) + "\n")


def append_entry(entry: LexiconEntry) -> None:
    """追加一条到词库。"""
    p = _resolve_lexicon_path()
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry.to_dict(), ensure_ascii=False) + "\n")


# ===== 被动监听(异步,不影响主流程)=====
_candidate_counter: dict[str, int] = {}  # word -> count
_candidate_lock = asyncio.Lock()


async def observe_danmaku(text: str, *, is_sc: bool = False) -> None:
    """被动监听一条弹幕,提取候选词。
    
    调用方:在 EV_DANMAKU_RECV 事件订阅里调用,不阻塞主流程。
    """
    if is_sc and not _config().get("SOCIAL_LEARNING_SKIP_SC", True):
        # 配置为不跳过 SC 时也学习
        pass
    elif is_sc:
        return  # 默认跳过 SC(隐私 + 私人内容)
    
    if not text:
        return
    
    # 提取候选词
    words = set(_WORD_RE.findall(text))
    for w in words:
        if _is_candidate_word(w):
            async with _candidate_lock:
                _candidate_counter[w] = _candidate_counter.get(w, 0) + 1


# ===== 后台沉淀任务 =====
async def _periodic_consolidate_loop() -> None:
    """每 5 分钟跑一次:把高频候选词喂给 LLM 判定。"""
    while True:
        try:
            await asyncio.sleep(300)  # 5min
            await _consolidate_once()
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.exception(f"[learning] consolidate error: {e}")
            await asyncio.sleep(60)


async def _consolidate_once() -> None:
    """单次沉淀流程。"""
    cfg = _config()
    min_freq = int(cfg.get("SOCIAL_LEARNING_MIN_FREQ", 3))
    max_size = int(cfg.get("SOCIAL_LEARNING_MAX_LEXICON_SIZE", 200))
    ttl_days = int(cfg.get("SOCIAL_LEARNING_TTL_DAYS", 30))
    
    # 1) 取候选(读计数器)
    async with _candidate_lock:
        candidates = {w: c for w, c in _candidate_counter.items() if c >= min_freq}
        _candidate_counter.clear()
    
    if not candidates:
        # 即使没新候选,也要做淘汰
        _prune_expired(ttl_days)
        return
    
    logger.info(f"[learning] consolidating {len(candidates)} candidates")
    
    # 2) 喂 LLM 判定(可选,失败也无所谓)
    accepted = []
    for word, freq in candidates.items():
        try:
            verdict = await _ask_llm_to_judge(word, freq)
        except Exception as e:
            logger.warning(f"[learning] LLM judge failed for {word}: {e}")
            verdict = "skipped"
        
        if verdict == "accepted":
            entry = LexiconEntry(
                word=word,
                meaning="",  # 等下次 LLM 溯源时填充
                tone="neutral",
                first_seen_ts=time.time(),
                use_count=freq,
                last_used_ts=time.time(),
                status="accepted",
            )
            append_entry(entry)
            accepted.append(word)
        # skipped / rejected → 不入库
    
    # 3) 淘汰过期
    _prune_expired(ttl_days)
    
    # 4) 上限裁剪
    _cap_lexicon_size(max_size)
    
    if accepted:
        logger.info(f"[learning] accepted {len(accepted)} new words: {accepted[:5]}...")


# LLM 判定器（可注入）：bootstrap 时由主程序注入 brain，未注入则跳过判定
# 签名: async def judge(prompt: str) -> str  （返回 LLM 文本）
_judge_fn = None


def set_llm_judge(fn) -> None:
    """注入 LLM 判定函数（bootstrap 时传 brain.chat_stream 的包装）。"""
    global _judge_fn
    _judge_fn = fn


async def _ask_llm_to_judge(word: str, freq: int) -> str:
    """调 LLM 判定:这个词学不学,什么意思,什么 tone。

    失败时返回 skipped,不阻塞主流程。未注入判定器时也返回 skipped
    （词不计入词库,不影响主流程）。
    """
    cfg = _config()
    if not cfg.get("SOCIAL_LEARNING_ENABLED", True):
        return "skipped"

    if _judge_fn is None:
        return "skipped"

    prompt = f"""判断这个直播弹幕里的词是否值得学到 AI 主播的词库里。

词: {word}
最近 7 天出现频次: {freq}

回答格式(只输出三行):
verdict: accepted / skipped
tone: 夸赞 / 嘲讽 / 中性 / 夸张 / 自嘲
meaning: 简短解释(accepted 时填写,1~2 句)"""

    try:
        text = await _judge_fn(prompt)
        text = str(text or "")
        for line in text.splitlines():
            line = line.strip().lower()
            if line.startswith("verdict:"):
                v = line.split(":", 1)[1].strip()
                if v in ("accepted", "skipped"):
                    return v
        return "skipped"
    except Exception as e:
        logger.debug(f"[learning] _ask_llm_to_judge error: {e}")
        return "skipped"


def _prune_expired(ttl_days: int) -> None:
    """淘汰 ttl_days 天未用的词。"""
    entries = load_lexicon()
    cutoff = time.time() - ttl_days * 86400
    kept = [e for e in entries if e.last_used_ts > cutoff or e.use_count > 5]
    if len(kept) < len(entries):
        save_lexicon(kept)


def _cap_lexicon_size(max_size: int) -> None:
    """词库超限时,淘汰低频的。"""
    entries = load_lexicon()
    if len(entries) <= max_size:
        return
    entries.sort(key=lambda e: (e.use_count, e.last_used_ts), reverse=True)
    save_lexicon(entries[:max_size])


# ===== 主动召回(给 prompt 注入用)=====
def recall_lexicon(
    current_emotion: str = "neutral",
    top_k: int = 12,
) -> list[LexiconEntry]:
    """按"当前情绪匹配 + 近期使用 + 高频"召回 Top-K。
    
    调用方在 build_system_prompt 时调用,得到词表后渲染成 prompt 片段。
    """
    entries = load_lexicon()
    if not entries:
        return []
    
    # 情绪方向
    emotion_tones = {
        "happy": {"夸赞", "中性"},
        "excited": {"夸张", "夸赞"},
        "neutral": {"中性", "夸赞", "夸张", "自嘲"},
        "tired": {"中性", "自嘲"},
        "sad": {"自嘲", "中性"},
        "angry": {"嘲讽", "中性"},
    }.get(current_emotion, {"中性"})
    
    scored = []
    now = time.time()
    for e in entries:
        if e.status != "accepted":
            continue
        s = 0.0
        if e.tone in emotion_tones:
            s += 0.4
        if now - e.last_used_ts < 7 * 86400:
            s += 0.3
        s += min(e.use_count / 50.0, 0.3)
        scored.append((s, e))
    
    scored.sort(key=lambda x: x[0], reverse=True)
    return [e for _, e in scored[:top_k]]


def build_lexicon_prompt_patch(current_emotion: str = "neutral") -> str:
    """把召回的词表渲染成 prompt 片段。"""
    words = recall_lexicon(current_emotion=current_emotion)
    if not words:
        return ""
    
    lines = ["## 弹幕流行词（可视情况自然用，别硬塞）", ""]
    for w in words:
        if w.meaning:
            lines.append(f"- {w.word}:{w.meaning}({w.tone})")
        else:
            lines.append(f"- {w.word}({w.tone})")
    return "\n".join(lines)


# ===== 配置 =====
def _config() -> dict:
    """读 ev.utils.config 的当前值。失败时返回空字典(全默认)。"""
    try:
        from ev.utils import config
        # ev.utils.config 是个对象,所有字段都大写
        return {
            "SOCIAL_LEARNING_ENABLED": getattr(config.cfg, "SOCIAL_LEARNING_ENABLED", True),
            "SOCIAL_LEARNING_SKIP_SC": getattr(config.cfg, "SOCIAL_LEARNING_SKIP_SC", True),
            "SOCIAL_LEARNING_MIN_FREQ": getattr(config.cfg, "SOCIAL_LEARNING_MIN_FREQ", 3),
            "SOCIAL_LEARNING_MAX_LEXICON_SIZE": getattr(config.cfg, "SOCIAL_LEARNING_MAX_LEXICON_SIZE", 200),
            "SOCIAL_LEARNING_TTL_DAYS": getattr(config.cfg, "SOCIAL_LEARNING_TTL_DAYS", 30),
            "SOCIAL_LEARNING_PURGE_TODAY": getattr(config.cfg, "SOCIAL_LEARNING_PURGE_TODAY", False),
        }
    except Exception:
        return {}


# ===== 一键清空 =====
def purge_today() -> None:
    """清空词库(调试用)。"""
    p = _resolve_lexicon_path()
    if p.exists():
        p.unlink()
    logger.info("[learning] lexicon purged")
