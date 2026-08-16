"""话题进化（原 EvolutionEngine 的话题职责拆分）。

从互动中提炼新话题，追加 topics.yml 并注入运行中的 ProactiveEngine
（重启后由 topics.yml 自动加载，双重生效）。
"""

from __future__ import annotations

import hashlib

import yaml

from src.utils import console

from .metrics import EVENT_TOPIC_ADDED, METRICS
from .prompts import CFG


def _make_topic_id(concept: str) -> str:
    """根据话题内容生成稳定 id（learned_ + 内容哈希前 8 位）。"""
    digest = hashlib.md5(concept.encode("utf-8")).hexdigest()[:8]
    return f"learned_{digest}"


class TopicEvolution:
    """话题提炼：追加 topics.yml + 注入运行中的主动引擎（双重生效）。"""

    def append_topics(self, new_topics: list[dict], proactive) -> None:
        """把新话题追加进 topics.yml，并注入运行中的 ProactiveEngine。"""
        try:
            with open(CFG.topics_path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
        except (OSError, ValueError) as e:
            console.warn(f"[进化] 读取话题文件失败：{e}")
            return
        existing = data.get("topics") or []
        seen = {t.get("concept") for t in existing}
        added: list[dict] = []
        for t in new_topics:
            concept = (t.get("concept") or "").strip()
            if not concept or concept in seen:
                continue
            tags = t.get("tags")
            if isinstance(tags, list):
                tags = [str(x) for x in tags][:4]
            else:
                tags = []
            entry = {
                "id": _make_topic_id(concept),
                "category": CFG.topic_category,
                "concept": concept,
                "tags": tags,
                "cooldown_minutes": CFG.topic_cooldown_minutes,
            }
            existing.append(entry)
            added.append(entry)
            seen.add(concept)
        if not added:
            return
        try:
            with open(CFG.topics_path, "w", encoding="utf-8") as f:
                yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)
        except OSError as e:
            console.warn(f"[进化] 写入话题文件失败：{e}")
            return
        # 注入运行中的主动引擎：当前会话立即可用，重启后 topics.yml 兜底
        if proactive is not None and hasattr(proactive, "add_topic_seeds"):
            try:
                proactive.add_topic_seeds(added)
            except Exception:
                pass
        console.ok(f"[进化] 话题进化：新增 {len(added)} 个话题")
        METRICS.incr(EVENT_TOPIC_ADDED, len(added))
