"""观众画像（原 EvolutionEngine 的画像职责拆分）。

把复盘提炼的「观众是谁 / 偏好 / 主播应长期保持的行为」等长期事实落盘
data/evolution_profile.json，由 llm_brain 每轮按关键词召回注入系统提示
（对标 hermes 的 USER.md/MEMORY.md），与 memU 向量记忆互为补充。
"""

from __future__ import annotations

import json
import os
import time

from ev.utils import config, console

from ._utils import JsonStore
from .metrics import EVENT_PROFILE_ADDED, METRICS
from .prompts import CFG


# 观众画像存储（data/evolution_profile.json）：复用通用 JSON 存取，超限保尾
_PROFILE_STORE = JsonStore(
    CFG.profile_path, label="观众画像", max_items=CFG.profile_max)

# 画像修正历史（data/evolution_profile_history.jsonl）：被 replace/remove 的
# 旧事实可回溯（5.7，对标 memory_tool 的 replace/remove 语义，不丢失数据）
_PROFILE_HISTORY_PATH = os.path.join(
    config.cfg.DATA_ROOT, "evolution_profile_history.jsonl")


def _load_profile() -> list[dict]:
    """读取观众画像条目列表（文件缺失/损坏时返回空列表）。"""
    return _PROFILE_STORE.load()


def _save_profile(items: list[dict]) -> None:
    """覆写观众画像条目列表。"""
    _PROFILE_STORE.save(items)


def _append_history(entry: dict) -> None:
    """被修正/移除的画像旧事实追加进历史账本（JSONL，失败静默）。"""
    try:
        os.makedirs(os.path.dirname(_PROFILE_HISTORY_PATH), exist_ok=True)
        with open(_PROFILE_HISTORY_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as e:
        console.warn(f"[进化] 画像历史写入失败：{e}")


def _remove_fact(items: list[dict], owner: str, fact: str) -> bool:
    """删除同 owner 的指定画像条目（精确匹配 fact），返回是否删除。"""
    removed = False
    keep: list[dict] = []
    for it in items:
        if it.get("owner") == owner and it.get("fact") == fact:
            removed = True
            continue
        keep.append(it)
    if removed:
        items[:] = keep
    return removed


def _replace_fact(items: list[dict], owner: str,
                  old_fact: str, new_fact: str) -> bool:
    """同 key（owner）覆盖旧事实为新事实（保留 created），返回是否覆盖。"""
    replaced = False
    for it in items:
        if it.get("owner") == owner and it.get("fact") == old_fact:
            it["fact"] = new_fact
            replaced = True
    return replaced


class ProfileEvolution:
    """观众画像：提炼长期事实落盘（文本去重，超限丢弃最旧）。"""

    def append_profile(self, profiles: list) -> None:
        """按 action 落地观众画像（5.7）：add 追加 / replace 覆盖 / remove 删除。

        落盘结构不变（{owner, fact, created}，兼容旧记录）；被覆盖/移除的
        旧事实写入 evolution_profile_history.jsonl 供回溯；replace/remove 后
        旧条目不再注入（同 owner 最新事实优先，用户纠正优先于沉淀）。
        """
        from tools.memory import memory
        items = _load_profile()
        default_owner = memory.get_manager().default_user_id
        added = 0
        changed = 0
        for p in profiles:
            if not isinstance(p, dict):
                continue
            action = (p.get("action") or "add").strip().lower()
            fact = (p.get("fact") or "").strip()
            if not fact:
                continue
            owner = (p.get("owner") or "").strip()[:32] or default_owner
            if action == "remove":
                if _remove_fact(items, owner, fact):
                    _append_history({
                        "ts": time.time(), "owner": owner,
                        "action": "remove", "fact": fact,
                    })
                    changed += 1
                continue
            if action == "replace":
                old_fact = (p.get("old_fact") or "").strip()
                if old_fact and _replace_fact(items, owner, old_fact, fact):
                    _append_history({
                        "ts": time.time(), "owner": owner,
                        "action": "replace", "old_fact": old_fact, "fact": fact,
                    })
                    changed += 1
                    continue
                # old_fact 未命中 → 降级为新增（保持 add 语义）
            # add 分支保持原语义：fact 全局去重（跨 owner 相同事实只存一条）
            if any(it.get("fact") == fact for it in items):
                continue
            items.append({"owner": owner, "fact": fact, "created": time.time()})
            added += 1
        if not added and not changed:
            return
        if len(items) > CFG.profile_max:
            items = items[-CFG.profile_max:]
        _save_profile(items)
        parts = [f"新增 {added} 条"] if added else []
        if changed:
            parts.append(f"修正/移除 {changed} 条")
        console.ok(f"[进化] 观众画像：{'，'.join(parts)}（共 {len(items)} 条）")
        if added:
            METRICS.incr(EVENT_PROFILE_ADDED, added)
