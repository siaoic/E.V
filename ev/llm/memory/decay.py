"""时间衰减：按 topic 差异化半衰期（作用于任意 MemoryBackend）。

设计（借鉴 Firefly TOPIC_DECAY_RATES，保持 vtuber 精简风格）：
- 每个 topic 有独立半衰期与最低置信度门槛；
- 身份类几乎不衰（365 天半衰期），事件/情绪/日程类快速遗忘；
- confidence = raw * 0.5 ** (age_days / half_life_days)；
- 低于 min_confidence 直接删除，否则写回新置信度；
- 纯函数实现，后端无关：LiteMemoryBackend.decay() 直接委托，
  未来 memu 后端也能复用。

遗忘增强（对标 Firefly 六维度体系，1/2/5 已有，3/4/6 本模块补齐）：
1. 差异化衰减率（TOPIC_DECAY half_life_days）
2. 差异化衰减阈值（DecayRule.min_confidence）
3. 时效语言分级（staleness_tier：active/recent/stale/archived，注入标注用）
4. 竞争衰减（competitive_decay：同 topic 新事实压制旧记忆）
5. 记忆刷新（LiteMemoryBackend.recall 命中即 touch，刷新 last_accessed）
6. 后台衰减循环（decay_loop：定时对 backend 应用差异化衰减）
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from ev.llm.memory.base import MemoryBackend


@dataclass(frozen=True)
class DecayRule:
    half_life_days: float
    min_confidence: float


# 各主题差异化衰减规则
TOPIC_DECAY: dict[str, DecayRule] = {
    "identity":       DecayRule(365.0, 0.5),   # 身份画像：基本不衰
    "preference":     DecayRule(90.0, 0.4),    # 偏好：缓慢遗忘
    "habit":          DecayRule(60.0, 0.3),    # 习惯
    "relationship":   DecayRule(180.0, 0.5),   # 人际关系
    "experience":     DecayRule(365.0, 0.3),   # 亲历体验
    "work_project":   DecayRule(30.0, 0.2),    # 工作项目
    "learning_skill": DecayRule(90.0, 0.3),    # 学习技能
    "event_social":   DecayRule(21.0, 0.3),    # 社交事件
    "event_travel":   DecayRule(30.0, 0.2),    # 旅行事件
    "emotion":        DecayRule(7.0, 0.2),     # 情绪：快速消散
    "schedule":       DecayRule(3.0, 0.0),     # 日程：最易过期
    "health_condition": DecayRule(14.0, 0.2),  # 健康状况
    "general":        DecayRule(60.0, 0.3),    # 兜底
}


def decay_confidence(confidence: float, topic: str, age_days: float) -> float:
    """按 topic 半衰期计算衰减后的置信度。"""
    rule = TOPIC_DECAY.get(topic, TOPIC_DECAY["general"])
    return confidence * (0.5 ** (age_days / rule.half_life_days))


async def decay_stale_memories(
    backend: MemoryBackend,
    *,
    now: float | None = None,
    limit: int = 100000,
) -> int:
    """对 backend 全部记忆应用差异化衰减，返回清理条数。

    每条记忆：
    - 新置信度低于该 topic 门槛 → delete；
    - 否则 update(confidence=新值) 写回（差值 > 0.001 才写，避免无效 IO）。
    """
    now = now if now is not None else time.time()
    deleted = 0
    for mem in await backend.list(limit=limit):
        topic = mem.get("topic") or "general"
        rule = TOPIC_DECAY.get(topic, TOPIC_DECAY["general"])
        created = float(mem.get("created_at") or now)
        age_days = max(0.0, (now - created) / 86400.0)
        new_conf = decay_confidence(float(mem.get("confidence") or 0.8), topic, age_days)
        if new_conf < rule.min_confidence:
            if await backend.delete(int(mem["id"])):
                deleted += 1
        elif abs(new_conf - float(mem.get("confidence") or 0.8)) > 0.001:
            await backend.update(int(mem["id"]), confidence=new_conf)
    return deleted


# ---- 遗忘增强（对标 Firefly 六维度体系） ----

# 时效语言分级边界（天）：active(<7) / recent(<90) / stale(<365) / archived(>=365)
_STALENESS_ACTIVE_DAYS = 7
_STALENESS_RECENT_DAYS = 90
_STALENESS_STALE_DAYS = 365

# 时效分级 → 注入 prompt 的中文时效标签
STALENESS_TIER_LABEL = {
    "active": "目前",
    "recent": "最近",
    "stale": "此前",
    "archived": "很久以前",
}

# 竞争衰减因子：同 topic 新记忆入库时，旧记忆置信度乘此因子（<1）弱化
_COMPETITIVE_FACTOR = 0.9

# 竞争衰减适用主题（可替换事实）：身份/关系/体验/情绪类不参与（防误伤）
_COMPETITIVE_TOPICS = frozenset({"preference", "habit", "schedule", "work_project"})


def staleness_tier(age_days: float) -> str:
    """时效语言分级：active(<7d) / recent(<90d) / stale(<365d) / archived(>=365d)。

    供召回注入 prompt 时标注记忆新鲜度（对标 Firefly _get_staleness_tier）。
    """
    if age_days < _STALENESS_ACTIVE_DAYS:
        return "active"
    if age_days < _STALENESS_RECENT_DAYS:
        return "recent"
    if age_days < _STALENESS_STALE_DAYS:
        return "stale"
    return "archived"


async def competitive_decay(
    backend: MemoryBackend,
    *,
    topic: str,
    new_id: int,
    namespace: str | None = None,
    factor: float = _COMPETITIVE_FACTOR,
) -> int:
    """竞争衰减：新事实入库后压制同 topic 旧记忆置信度，返回受影响条数。

    用于可替换事实（偏好/习惯/日程等）：新事实出现即旧事实弱化，
    随自然衰减逐渐退出注入。身份/关系/体验类不参与，防误伤。
    """
    lowered = 0
    for mem in await backend.list(namespace=namespace, limit=100000):
        if int(mem["id"]) == int(new_id):
            continue
        if (mem.get("topic") or "general") != topic:
            continue
        conf = float(mem.get("confidence") or 0.8) * factor
        if await backend.update(int(mem["id"]), confidence=conf):
            lowered += 1
    return lowered


def should_compete(topic: str) -> bool:
    """该主题是否参与竞争衰减（可替换事实类）。"""
    return topic in _COMPETITIVE_TOPICS


async def decay_loop(backend: MemoryBackend, interval_sec: float = 6 * 3600) -> None:
    """后台衰减循环：每 interval_sec 对 backend 应用一次差异化衰减。

    供启用 LiteMemoryBackend 的进程启动（默认 6 小时，与 tools.memory
    的 decay_loop 节奏一致）；任何一次失败静默跳过，不中断循环。
    """
    while True:
        try:
            removed = await decay_stale_memories(backend)
            if removed:
                from ev.utils import console

                console.dim(f"记忆衰减：清理 {removed} 条过期记忆")
        except Exception:
            pass
        await asyncio.sleep(interval_sec)
