"""记忆衰减增强测试：时效分级 / 竞争衰减 / 差异化衰减清理。"""
import asyncio
import time

from src.llm.memory.decay import (
    STALENESS_TIER_LABEL,
    competitive_decay,
    decay_stale_memories,
    should_compete,
    staleness_tier,
)


class FakeBackend:
    """内存版 MemoryBackend 替身（list/delete/update 三接口）。"""

    def __init__(self, mems):
        self._mems = [dict(m) for m in mems]

    async def list(self, namespace=None, limit=100000):
        return [dict(m) for m in self._mems]

    async def delete(self, memory_id):
        for m in self._mems:
            if m["id"] == memory_id:
                self._mems.remove(m)
                return True
        return False

    async def update(self, memory_id, *, content=None, confidence=None):
        for m in self._mems:
            if m["id"] == memory_id:
                if confidence is not None:
                    m["confidence"] = confidence
                if content is not None:
                    m["content"] = content
                return True
        return False


class TestStalenessTier:
    """维度三：时效语言分级（对标 Firefly _get_staleness_tier）。"""

    def test_active_under_7_days(self):
        assert staleness_tier(0) == "active"
        assert staleness_tier(6) == "active"

    def test_boundary_7_days(self):
        assert staleness_tier(6.99) == "active"
        assert staleness_tier(7) == "recent"

    def test_boundary_90_days(self):
        assert staleness_tier(89) == "recent"
        assert staleness_tier(90) == "stale"

    def test_boundary_365_days(self):
        assert staleness_tier(364) == "stale"
        assert staleness_tier(365) == "archived"

    def test_labels_cover_all_tiers(self):
        for tier in ("active", "recent", "stale", "archived"):
            assert STALENESS_TIER_LABEL[tier]


class TestCompetitiveDecay:
    """维度四：竞争衰减（同 topic 新旧压制）。"""

    def test_same_topic_lowered(self):
        async def _inner():
            now = time.time()
            backend = FakeBackend([
                {"id": 1, "topic": "preference", "confidence": 0.9, "created_at": now - 100},
                {"id": 2, "topic": "preference", "confidence": 0.7, "created_at": now - 50},
                {"id": 3, "topic": "identity", "confidence": 0.9, "created_at": now - 10},
            ])
            n = await competitive_decay(backend, topic="preference", new_id=4)
            assert n == 2
            by_id = {m["id"]: m for m in backend._mems}
            assert by_id[1]["confidence"] == 0.9 * 0.9
            assert by_id[2]["confidence"] == 0.7 * 0.9
            # 异 topic（identity）不受影响
            assert by_id[3]["confidence"] == 0.9

        asyncio.run(_inner())

    def test_new_id_skipped(self):
        async def _inner():
            backend = FakeBackend([
                {"id": 5, "topic": "habit", "confidence": 0.8, "created_at": 1},
            ])
            n = await competitive_decay(backend, topic="habit", new_id=5)
            assert n == 0
            assert backend._mems[0]["confidence"] == 0.8

        asyncio.run(_inner())

    def test_should_compete_only_replaceable(self):
        assert should_compete("preference")
        assert should_compete("schedule")
        assert not should_compete("identity")
        assert not should_compete("relationship")
        assert not should_compete("emotion")


class TestDecayStaleMemories:
    """维度一/二：差异化衰减率 + 差异化阈值。"""

    def test_below_threshold_deleted(self):
        """emotion（7 天半衰）30 天前记忆：置信度跌破 0.2 门槛 → 删除。"""
        async def _inner():
            now = time.time()
            backend = FakeBackend([
                {"id": 1, "topic": "emotion", "confidence": 0.8,
                 "created_at": now - 30 * 86400},
                {"id": 2, "topic": "emotion", "confidence": 0.8,
                 "created_at": now},
            ])
            deleted = await decay_stale_memories(backend, now=now)
            assert deleted == 1
            assert [m["id"] for m in backend._mems] == [2]

        asyncio.run(_inner())

    def test_identity_kept_and_updated(self):
        """identity（365 天半衰）30 天前记忆：高于 0.5 门槛 → 只写回新置信度。"""
        async def _inner():
            now = time.time()
            backend = FakeBackend([
                {"id": 1, "topic": "identity", "confidence": 0.8,
                 "created_at": now - 30 * 86400},
            ])
            deleted = await decay_stale_memories(backend, now=now)
            assert deleted == 0
            mem = backend._mems[0]
            assert mem["confidence"] < 0.8  # 已衰减但未删除
            assert mem["confidence"] > 0.5

        asyncio.run(_inner())
