"""主动发言引擎的策略/常量/话题加载 + 话题挑选 + 心跳时机。

模块级常量与函数，core.py 中的 ProactiveEngine 类转发调用。
"""

from __future__ import annotations

import os
import random
import sys
import time
from collections import deque
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import yaml

from ev.utils import console

# —— 自主开口（LLM 自主决定，无时间门槛）——
# 主模型输出以下任一标记视为「此刻不想说」→ 保持沉默（幂等匹配）
_SILENT_MARKERS = (
    "<SILENT>", "SILENT", "沉默", "保持沉默", "不想说", "此刻不想说",
    "暂无", "无", "没有", "算了", "不了", "NONE", "NULL",
)

# —— 话题类别权重（对标 topic_manager.TOPIC_WEIGHTS）——
_TOPIC_WEIGHTS: Dict[str, float] = {
    "neuro": 0.35,
    "neuro_fact": 0.25,
    "neuro_story": 0.25,
    "learned": 0.15,
}
# 近期已聊类别的权重惩罚（窗口 3，避免连续同主题）
_RECENT_TYPE_PENALTY: float = 0.25
_RECENT_TYPE_WINDOW: int = 3

# —— 互动率评分（对标 TopicManager._get_available_candidates）——
_INTERACTION_LOW: float = 0.3
_INTERACTION_MID: float = 0.6

# —— 话题活跃期（对标 state.ActiveTopicState）——
_ACTIVE_TOPIC_TIMEOUT: float = 120.0

# —— 静默兜底（防冷场）——
_FORCE_SPEAK_QUIET: float = 25.0

# 话题种子路径：优先 configs/profiles/default/topics.yaml，回退 src/llm/topics.yml
_VTUBER_ROOT = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", ".."))
_NEW_TOPICS_PATH = os.path.join(
    _VTUBER_ROOT, "configs", "profiles", "default", "topics.yaml")


def _resolve_topics_path() -> str:
    if getattr(sys, "frozen", False):
        new_pkg = os.path.join(sys._MEIPASS, "configs", "profiles",
                               "default", "topics.yaml")
        if os.path.isfile(new_pkg):
            return new_pkg
        return os.path.join(sys._MEIPASS, "src", "llm", "topics.yml")
    if os.path.isfile(_NEW_TOPICS_PATH):
        return _NEW_TOPICS_PATH
    # 老路径：本模块所在目录是 ev/llm/proactive/，源文件在 src/llm/topics.yml
    return os.path.normpath(os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "..", "..", "..", "src", "llm", "topics.yml"))


_TOPICS_PATH = _resolve_topics_path()


def _load_topic_seeds() -> List[dict]:
    """从 topics.yml 加载话题种子（对标 TopicStore._load）。"""
    try:
        with open(_TOPICS_PATH, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except (OSError, ValueError) as e:
        console.error(f"[主动] 加载话题种子失败：{e}（话题管线停用）")
        return []
    seeds = []
    for entry in data.get("topics", []):
        concept = entry.get("concept", entry.get("content", ""))
        if not concept:
            continue
        seeds.append({
            "id": entry["id"],
            "category": entry.get("category", "misc"),
            "concept": concept,
            "tags": entry.get("tags", []),
            "cooldown_minutes": int(entry.get("cooldown_minutes", 7)),
        })
    return seeds


# ---------- 话题挑选（原 _pick_topic，模块函数接受 engine self）----------

def _pick_topic(self) -> Optional[dict]:
    """按权重机制挑选一个话题种子（对标 TopicManager.get_next_topic）。

    1. 类别权重；2. 近期类别惩罚；3. 独立冷却；4. 互动率加权；
    5. 全部冷却中放宽冷却强制选一个。
    """
    weights = dict(_TOPIC_WEIGHTS)
    now = datetime.now()
    available: Dict[str, List[dict]] = {}
    for topic in self._topic_seeds:
        last = self._topic_last_used.get(topic["id"])
        if last is not None:
            elapsed = (now - last).total_seconds()
            if elapsed < topic["cooldown_minutes"] * 60:
                continue
        available.setdefault(topic["category"], []).append(topic)
    if not available:
        def _remain(t):
            last = self._topic_last_used.get(t["id"])
            if last is None:
                return 0.0
            elapsed = (now - last).total_seconds()
            return max(0.0, t["cooldown_minutes"] * 60 - elapsed)
        coolest = sorted(self._topic_seeds, key=_remain)[:10]
        fallback = random.choice(coolest)
        console.dim(
            f"[主动] 话题全部冷却中，放宽冷却强制选话题："
            f"{fallback['category']}/{fallback['id']}")
        return fallback
    filtered = {cat: weights.get(cat, 0.05) for cat in available}
    for recent in self._recent_categories:
        if recent in filtered:
            filtered[recent] *= _RECENT_TYPE_PENALTY
    total = sum(filtered.values())
    if total <= 0:
        return random.choice(self._topic_seeds)
    cats = list(filtered.keys())
    cat_weights = [filtered[c] / total for c in cats]
    chosen_cat = random.choices(cats, weights=cat_weights, k=1)[0]
    pool = available[chosen_cat]
    pool_weights = []
    for t in pool:
        stats = self._topic_stats.get(t["id"])
        w = 1.0
        if stats is not None and stats["use"] >= 2:
            engagement = stats["engaged"] / stats["use"]
            if engagement < 0.3:
                w = _INTERACTION_LOW
            elif engagement < 0.5:
                w = _INTERACTION_MID
        pool_weights.append(w)
    chosen = random.choices(pool, weights=pool_weights, k=1)[0]
    self._recent_categories.append(chosen_cat)
    self._topic_last_used[chosen["id"]] = now
    return chosen


# ---------- 心跳日志 + 精确唤醒时机 ----------

def _log_heartbeat(self) -> None:
    """心跳日志：显示距上次互动与当前开口机会状态。"""
    quiet = time.time() - self.last_interaction
    extra = ""
    if self.active_topic is not None:
        remain = _ACTIVE_TOPIC_TIMEOUT - (
            time.time() - self.active_topic["started_at"].timestamp())
        if remain > 0:
            extra = f" | 话题活跃中({remain:.0f}s后可触发)"
        else:
            extra = " | 话题活跃中(即将超时)"
    elif self.speaking:
        extra = " | 发言中"
    console.dim(f"[心跳] 静默 {quiet:.0f}s，由 LLM 自主决定是否开口{extra}")


def next_wake_in(self) -> float:
    """距下一次「自主开口机会」的秒数（单次精确唤醒，无周期轮询）。"""
    now = time.time()
    if self.speaking or not self._queue.empty():
        return max(1.0, _ACTIVE_TOPIC_TIMEOUT)
    if self.active_topic is not None:
        remain = _ACTIVE_TOPIC_TIMEOUT - (
            now - self.active_topic["started_at"].timestamp())
        return max(0.0, remain)
    return random.uniform(
        self.cfg.RESPONSE_INTERVAL_MIN, self.cfg.RESPONSE_INTERVAL_MAX)


def _begin_topic(self, topic: dict) -> None:
    """进入话题活跃期并记录使用次数。"""
    self.active_topic = {
        "topic_id": topic["id"],
        "started_at": datetime.now(),
        "user_engaged": False,
    }
    self._topic_stats.setdefault(topic["id"], {"use": 0, "engaged": 0})["use"] += 1
