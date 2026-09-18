# -*- coding: utf-8 -*-
"""WorldStateCache 双视图（实时 / 延迟）行为契约。

覆盖实施计划 W5 的关键机制：
- 两个视图各自独立 dirty / last_injected_at，互不影响；
- build_blocks 的三条注入规则在延迟视图上同样成立；
- drop 一并丢弃两个视图。
"""

from src.worlds.state_cache import WorldStateCache


def make_cache(stale: float = 45.0) -> WorldStateCache:
    return WorldStateCache(stale_after_seconds=lambda: stale)


def test_realtime_and_delayed_slots_are_independent():
    cache = make_cache()
    assert cache.update("minecraft", "实时快照 A", now=0.0) is True
    assert cache.update("minecraft", "延迟快照 A", now=0.0, delayed=True) is True

    assert cache.snapshot_of("minecraft") == "实时快照 A"
    assert cache.delayed_snapshot_of("minecraft") == "延迟快照 A"


def test_build_blocks_injects_views_independently():
    cache = make_cache()
    cache.update("minecraft", "实时快照", now=0.0)
    cache.update("minecraft", "延迟快照", now=0.0, delayed=True)

    # 两个视图同时 dirty → 同一次注入给出两段
    realtime = dict(cache.build_blocks(["minecraft"], now=1.0))
    delayed = dict(cache.build_blocks(["minecraft"], now=1.0, delayed=True))
    assert realtime == {"minecraft": "实时快照"}
    assert delayed == {"minecraft": "延迟快照"}

    # 注入后都变干净：立即再取为空
    assert cache.build_blocks(["minecraft"], now=2.0) == []
    assert cache.build_blocks(["minecraft"], now=2.0, delayed=True) == []


def test_stale_refresh_is_per_view():
    cache = make_cache(stale=45.0)
    cache.update("minecraft", "实时快照", now=0.0)
    cache.update("minecraft", "延迟快照", now=0.0, delayed=True)
    cache.build_blocks(["minecraft"], now=1.0)
    # 延迟视图比实时视图晚 29 秒才被消费，过期时刻随之错开
    cache.build_blocks(["minecraft"], now=30.0, delayed=True)

    # 到 47.0 时：实时视图距上次注入 46s ≥ 45s → 补注入；延迟视图仅 17s → 不注入
    blocks = cache.build_blocks(["minecraft"], now=47.0)
    assert blocks == [("minecraft", "实时快照")]
    assert cache.build_blocks(["minecraft"], now=47.0, delayed=True) == []
    # 延迟视图到 77.0 时也过期（47 - 30 = 17 < 45），补注入一次
    blocks = cache.build_blocks(["minecraft"], now=77.0, delayed=True)
    assert blocks == [("minecraft", "延迟快照")]


def test_delayed_view_absent_does_not_block_realtime():
    cache = make_cache()
    cache.update("pvz", "只有实时", now=0.0)
    realtime = dict(cache.build_blocks(["pvz"], now=1.0))
    delayed = cache.build_blocks(["pvz"], now=1.0, delayed=True)
    assert realtime == {"pvz": "只有实时"}
    assert delayed == []


def test_drop_clears_both_views():
    cache = make_cache()
    cache.update("minecraft", "实时", now=0.0)
    cache.update("minecraft", "延迟", now=0.0, delayed=True)
    cache.drop("minecraft")
    assert cache.snapshot_of("minecraft") == ""
    assert cache.delayed_snapshot_of("minecraft") == ""
