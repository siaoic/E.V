"""话术闭环（原 EvolutionEngine 的话术职责拆分）：建议 → 生效 → 回评。

- append_advice：话术建议追加 evolution_advice.md 存档 + 登记生效建议
  （生效建议由 llm_brain 注入系统提示，到期由复盘回评续期或移除）
- apply_advice_status：按复盘回评更新生效列表（keep 续期保留，否则移除）
"""

from __future__ import annotations

import os
import re
import time
import uuid
from datetime import datetime

from src.utils import config, console

from ._utils import JsonStore
from .metrics import EVENT_ADVICE_ADDED, EVENT_ADVICE_REMOVED, METRICS
from .prompts import CFG


# 生效话术建议存储（data/evolution_advice_active.json）：复用通用 JSON 存取
_ADVICE_STORE = JsonStore(CFG.advice_active_path, label="生效话术建议")


def _load_active_advice() -> list[dict]:
    """读取生效话术建议列表（文件缺失/损坏时返回空列表）。"""
    return _ADVICE_STORE.load()


def _save_active_advice(items: list[dict]) -> None:
    """覆写生效话术建议列表。"""
    _ADVICE_STORE.save(items)


def _append_active_advice(text: str) -> None:
    """登记一条生效中的话术建议（含 id/有效期/负反馈计数，到期由复盘回评去留）。"""
    now = time.time()
    _ADVICE_STORE.append({
        "id": uuid.uuid4().hex[:8],
        "text": text,
        "created": now,
        "expires": now + CFG.advice_ttl_seconds,
        "negative_hits": 0,
    })


def _advice_hit(advice_text: str, feedback_text: str) -> bool:
    """建议与负反馈是否存在 ≥4 字连续子串重叠（弱主题关联，无分词依赖）。

    纯中文句子无法可靠切词，改用 4 字滑窗重叠判定：反馈引用/复述了建议的
    行为片段（如观众吐槽「你唱歌前先别啰嗦」命中建议「唱歌前先跟观众互动」
    的「唱歌前先」）即视为同主题命中；双向包含也命中。方向不定，保守近似。
    """
    advice_text = (advice_text or "").strip()
    feedback_text = (feedback_text or "").strip()
    if not advice_text or not feedback_text:
        return False
    if advice_text in feedback_text or feedback_text in advice_text:
        return True
    if len(advice_text) < 4:
        return False
    for i in range(len(advice_text) - 3):
        if advice_text[i:i + 4] in feedback_text:
            return True
    return False


def bump_advice_negative_hits(feedback_text: str) -> int:
    """负反馈文本命中生效建议关键词时 negative_hits+1，返回命中条数（5.8.2）。

    命中判定：反馈文本与建议存在 ≥5 字子串重叠（见 _advice_hit）；
    未命中/文件读写失败返回 0，不影响主流程。
    """
    text = (feedback_text or "").strip()
    if not text:
        return 0
    items = _load_active_advice()
    if not items:
        return 0
    hit = 0
    for it in items:
        if _advice_hit(it.get("text") or "", text):
            it["negative_hits"] = int(it.get("negative_hits") or 0) + 1
            hit += 1
    if hit:
        _save_active_advice(items)
        console.dim(f"[进化] 话术负反馈：{hit} 条生效建议被观众负反馈命中")
    return hit


def drop_advice(key: str) -> bool:
    """立即废弃一条生效话术建议（5.8.1）：按 #id 或文本匹配，移除并留档。

    移除后建议不再注入系统提示、不再进入复盘回评（自然不再续期）；
    废弃记录追加进 evolution_advice.md 存档（可回溯，对标 memory 审批留痕）。
    """
    key = (key or "").strip()
    if not key:
        return False
    items = _load_active_advice()
    if not items:
        return False
    removed: dict | None = None
    if key.startswith("#"):
        target_id = key[1:].strip()
        removed = next((it for it in items if it.get("id") == target_id), None)
    else:
        removed = next(
            (it for it in items if (it.get("text") or "").strip() == key), None)
    if removed is None:
        return False
    _save_active_advice([it for it in items if it is not removed])
    # 废弃留档（追加到话术存档，便于回溯为什么这条被主播废弃）
    try:
        path = os.path.join(config.cfg.DATA_ROOT, "evolution_advice.md")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"\n## {stamp}（主播废弃，不再续期）\n~~{removed.get('text', '')}~~\n")
    except OSError as e:
        console.warn(f"[进化] 话术废弃留档失败：{e}")
    console.ok(f"[进化] 话术建议已废弃：{removed.get('text', '')}")
    METRICS.incr(EVENT_ADVICE_REMOVED)
    return True


def handle_advice_command(raw: str) -> tuple[bool, str]:
    """处理 !advice 主播命令（5.8）：!advice drop <文本> / !advice drop #<id>。

    返回 (是否命令, 结果文本)；非 !advice 开头返回 (False, "")（走正常对话）。
    """
    raw = (raw or "").strip()
    if not raw.startswith("!advice"):
        return False, ""
    cmd = raw[len("!advice"):].strip()
    if cmd.startswith("drop"):
        key = cmd[len("drop"):].strip()
        if not key:
            return True, "用法：!advice drop <建议文本> 或 !advice drop #<id>"
        return True, ("已废弃该话术建议，不再注入与续期" if drop_advice(key)
                      else "未找到匹配的生效话术建议（可用 !advice drop #<id> 精确指定）")
    return True, "用法：!advice drop <建议文本> 或 !advice drop #<id>"


def _pending_advice_text() -> list[str]:
    """返回已到期的话术建议文本（含负反馈命中次数，供复盘评估续期/移除）。"""
    now = time.time()
    out: list[str] = []
    for it in _load_active_advice():
        if (it.get("expires") or 0) <= now:
            text = (it.get("text") or "").strip()
            if not text:
                continue
            # 5.8.3：评估输入补充 negative_hits 列，命中多的倾向 keep=false
            hits = int(it.get("negative_hits") or 0)
            out.append(f"{text}（负反馈命中 {hits} 次）" if hits else text)
    return out


class AdviceEvolution:
    """话术闭环：沉淀建议 → 登记生效 → 复盘回评续期/移除。"""

    def append_advice(self, advice: str) -> None:
        """沉淀话术优化建议：追加 evolution_advice.md 存档 + 登记生效建议。

        生效建议由 llm_brain 注入系统提示；到期后由复盘回评续期或移除。
        """
        path = os.path.join(config.cfg.DATA_ROOT, "evolution_advice.md")
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
            with open(path, "a", encoding="utf-8") as f:
                f.write(f"\n## {stamp}\n{advice}\n")
        except OSError as e:
            console.warn(f"[进化] 写入话术建议失败：{e}")
            return
        _append_active_advice(advice)
        console.ok("[进化] 话术建议：已追加 evolution_advice.md 并登记生效")
        METRICS.incr(EVENT_ADVICE_ADDED)

    def apply_advice_status(self, status_list: list) -> None:
        """根据复盘回评更新生效建议：keep 续期保留，否则从生效列表移除。

        未被回评到的建议保持原状（仍到期，下轮复盘继续评估）。
        """
        verdicts = {}
        for it in status_list:
            if not isinstance(it, dict):
                continue
            # 剥离开启时展示用的「（负反馈命中 N 次）」后缀，保证与存档文本精确匹配
            # （协议要求 text 原样返回，但部分模型可能连后缀一起返回，需兼容）
            text = (it.get("text") or "").strip()
            text = re.sub(r"（负反馈命中 \d+ 次）$", "", text)
            verdicts[text] = bool(it.get("keep"))
        if not verdicts:
            return
        items = _load_active_advice()
        if not items:
            return
        kept: list[dict] = []
        removed = 0
        now = time.time()
        for it in items:
            text = (it.get("text") or "").strip()
            if text in verdicts:
                if verdicts[text]:
                    it["expires"] = now + CFG.advice_ttl_seconds
                    kept.append(it)
                else:
                    removed += 1
            else:
                kept.append(it)
        _save_active_advice(kept)
        if removed:
            console.ok(f"[进化] 话术回评：保留 {len(kept)} 条，移除 {removed} 条失效建议")
            METRICS.incr(EVENT_ADVICE_REMOVED, removed)
