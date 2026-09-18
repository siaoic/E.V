# -*- coding: utf-8 -*-
"""WorldManager 延迟双视图注入格式（W5 用户可见行为契约）。

验证 ``build_state_injection`` 在「supports_delayed + delayed_sources」世界上的
两段式输出，以及延迟事件经 ``delayed_seconds_for`` 的覆写入口。
"""

import pytest

from src.worlds.manager import WorldManager
from src.worlds.types import WorldDescriptor


def make_descriptor(name: str, supports_delayed: bool = False) -> WorldDescriptor:
    return WorldDescriptor(
        name=name,
        plugin_id="test-plugin",
        display_name=f"{name} 显示名",
        polls_changes=False,
        supports_delayed=supports_delayed,
    )


def build_manager(monkeypatch, delayed_sources) -> WorldManager:
    """构建管理器并在真实配置实例上打桩（pydantic 校验不接受任意替身对象）。"""

    from src.config.config import global_config

    manager = WorldManager()
    monkeypatch.setattr(global_config.worlds, "inject_state", True, raising=False)
    monkeypatch.setattr(global_config.worlds, "delayed_sources", list(delayed_sources), raising=False)
    monkeypatch.setattr(global_config.worlds, "delayed_seconds", 8, raising=False)
    return manager


def test_dual_view_injection_format(monkeypatch):
    manager = build_manager(monkeypatch, delayed_sources=["minecraft"])
    manager.register_world(make_descriptor("minecraft", supports_delayed=True))

    manager.update_state("minecraft", "你在 (1,2,3)，血量 12/20。")
    manager.update_state("minecraft", "你还在挖矿。", delayed=True)

    injection = manager.build_state_injection()
    assert "<world_state>" in injection
    assert "【minecraft 显示名】" in injection
    assert "【实时】你在 (1,2,3)，血量 12/20。" in injection
    assert "【观众此刻看到的画面】你还在挖矿。" in injection


def test_world_without_delayed_keeps_single_view(monkeypatch):
    manager = build_manager(monkeypatch, delayed_sources=[])
    manager.register_world(make_descriptor("bilibili"))

    manager.update_state("bilibili", "礼物 ×3。")
    injection = manager.build_state_injection()
    assert "【实时】" not in injection
    assert "【bilibili 显示名】\n礼物 ×3。" in injection


def test_delayed_not_listed_in_sources_stays_realtime_only(monkeypatch):
    """声明了 supports_delayed 但不在 delayed_sources 里 → 不产生延迟段，也不覆写延迟。"""
    manager = build_manager(monkeypatch, delayed_sources=["pvz"])
    manager.register_world(make_descriptor("minecraft", supports_delayed=True))

    manager.update_state("minecraft", "实时内容。")
    injection = manager.build_state_injection()
    assert "【观众此刻看到的画面】" not in injection
    assert manager.delayed_seconds_for("minecraft") is None


def test_delayed_seconds_for_overrides_events(monkeypatch):
    """delayed_sources 内的世界：事件延迟由基座统一套 OBS 延迟秒数。"""
    manager = build_manager(monkeypatch, delayed_sources=["minecraft"])
    manager.register_world(make_descriptor("minecraft", supports_delayed=True))

    assert manager.delayed_seconds_for("minecraft") == 8.0
