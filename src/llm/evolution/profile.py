"""观众画像（原 EvolutionEngine 的画像职责拆分）。

把复盘提炼的「观众是谁 / 偏好 / 主播应长期保持的行为」等长期事实落盘
data/evolution_profile.json，由 llm_brain 每轮按关键词召回注入系统提示
（对标 hermes 的 USER.md/MEMORY.md），与 memU 向量记忆互为补充。
"""

from __future__ import annotations

import time

from src.utils import console

from ._utils import JsonStore
from .metrics import EVENT_PROFILE_ADDED, METRICS
from .prompts import CFG


# 观众画像存储（data/evolution_profile.json）：复用通用 JSON 存取，超限保尾
_PROFILE_STORE = JsonStore(
    CFG.profile_path, label="观众画像", max_items=CFG.profile_max)


def _load_profile() -> list[dict]:
    """读取观众画像条目列表（文件缺失/损坏时返回空列表）。"""
    return _PROFILE_STORE.load()


def _save_profile(items: list[dict]) -> None:
    """覆写观众画像条目列表。"""
    _PROFILE_STORE.save(items)


class ProfileEvolution:
    """观众画像：提炼长期事实落盘（文本去重，超限丢弃最旧）。"""

    def append_profile(self, profiles: list) -> None:
        """把复盘提炼的观众画像条目追加进画像文件（文本去重，超限丢弃最旧）。"""
        from tools.memory import memory
        items = _load_profile()
        existing = {it.get("fact", "").strip() for it in items}
        added = 0
        for p in profiles:
            if not isinstance(p, dict):
                continue
            fact = (p.get("fact") or "").strip()
            if not fact or fact in existing:
                continue
            owner = ((p.get("owner") or "").strip()[:32]
                     or memory.get_manager().default_user_id)
            items.append({"owner": owner, "fact": fact, "created": time.time()})
            existing.add(fact)
            added += 1
        if not added:
            return
        if len(items) > CFG.profile_max:
            items = items[-CFG.profile_max:]
        _save_profile(items)
        console.ok(f"[进化] 观众画像：新增 {added} 条长期事实（共 {len(items)} 条）")
        METRICS.incr(EVENT_PROFILE_ADDED, added)
