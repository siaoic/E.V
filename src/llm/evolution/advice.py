"""话术闭环（原 EvolutionEngine 的话术职责拆分）：建议 → 生效 → 回评。

- append_advice：话术建议追加 evolution_advice.md 存档 + 登记生效建议
  （生效建议由 llm_brain 注入系统提示，到期由复盘回评续期或移除）
- apply_advice_status：按复盘回评更新生效列表（keep 续期保留，否则移除）
"""

from __future__ import annotations

import os
import time
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
    """登记一条生效中的话术建议（含有效期，到期由复盘回评决定去留）。"""
    now = time.time()
    _ADVICE_STORE.append({
        "text": text,
        "created": now,
        "expires": now + CFG.advice_ttl_seconds,
    })


def _pending_advice_text() -> list[str]:
    """返回已到期的话术建议文本（供复盘时让 LLM 评估是否续期）。"""
    now = time.time()
    return [
        (it.get("text") or "").strip()
        for it in _load_active_advice()
        if (it.get("expires") or 0) <= now
    ]


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
        verdicts = {
            (it.get("text") or "").strip(): bool(it.get("keep"))
            for it in status_list if isinstance(it, dict)
        }
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
