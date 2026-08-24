"""TR 15.1~15.5：5 个生产级 profile 都能解析、关键字段存在 & 类型正确。"""
from __future__ import annotations
import os
import pytest
from pathlib import Path

PROFILES_DIR = str(Path(__file__).resolve().parents[3] / "profiles")


def _resolve(name):
    from ev.kernel.profile import Profile
    p = Profile(os.path.join(PROFILES_DIR, f"{name}.yaml"))
    return p.resolve(builtins_root=PROFILES_DIR)


def _common_keys(r):
    # 返回关键字段元组用于断言
    return (
        r.get("name"),
        isinstance(r.get("plugins"), dict) and isinstance(r["plugins"].get("builtin"), list),
        isinstance(r.get("slots"), dict),
        isinstance(r.get("plugin_config"), dict),
    )


# TR 15.1 pet.yaml
def test_pet_profile():
    r = _resolve("pet")
    assert r["name"] == "pet"
    assert "echo_llm" in r["plugins"]["builtin"]
    assert "tts_edge" in r["plugins"]["builtin"]
    assert r["slots"]["model"] == "echo-default"
    assert r["plugin_config"]["tts_edge"]["voice"].startswith("zh-CN-")
    # pet 不应启用 danmaku slot
    assert r["slots"].get("danmaku") in (None, "null", "", False, "none")

# TR 15.2 live_bili.yaml
def test_live_bili_profile():
    r = _resolve("live_bili")
    assert r["name"] == "live_bili"
    assert "danmaku_bilibili" in r["plugins"]["builtin"]
    assert "danmaku_filter" in r["plugins"]["builtin"]
    assert "memory_long" in r["plugins"]["builtin"]
    assert "proactive_tick" in r["plugins"]["builtin"]
    assert r["slots"]["danmaku"] == "bilibili"
    assert r["slots"]["memory"] == "longterm"
    assert r["slots"]["proactive"] == "ticker"
    # 环境变量占位符：未设置时 ${BILI_ROOM_ID:-0} → "0"（空 env 时）
    assert str(r["plugin_config"]["danmaku_bilibili"]["room_id"]).isdigit()

# TR 15.3 live_dy.yaml (extends live_bili)
def test_live_dy_profile():
    r = _resolve("live_dy")
    assert r["name"] == "live_dy"
    # 继承的 plugins / emotion / tts / memory / proactive 都在
    assert r["slots"]["tts"] == "edge"
    assert r["slots"]["emotion"] == "rule-based"
    assert r["slots"]["memory"] == "longterm"
    assert r["slots"]["proactive"] == "ticker"
    # 覆盖的 danmaku = douyin
    assert r["slots"]["danmaku"] == "douyin"
    # 继承 plugin_config.memory_long.top_k = 8（非空）
    assert isinstance(r["plugin_config"]["memory_long"]["top_k"], int)

# TR 15.4 mcp_sandbox.yaml
def test_mcp_sandbox_profile():
    r = _resolve("mcp_sandbox")
    assert r["name"] == "mcp_sandbox"
    assert "mcp_client" in r["plugins"]["builtin"]
    assert "sandbox_safe" in r["plugins"]["builtin"]
    assert "evolution_day1" in r["plugins"]["builtin"]
    assert r["slots"]["mcp"] == "official-client"
    assert r["slots"]["sandbox"] == "safe-sandbox"
    assert r["slots"]["evolution"] == "day1-trace"
    # mcp_client.servers 包含 filesystem 和 time 2 个
    assert set(r["plugin_config"]["mcp_client"]["servers"].keys()) == {"filesystem", "time"}

# TR 15.5 full.yaml (多继承 live_bili + mcp_sandbox)
def test_full_profile():
    r = _resolve("full")
    assert r["name"] == "full"
    # 12 个 slot 全部都绑定（非 None 占位）
    assert r["slots"]["butler"] == "smart-butler"
    assert r["slots"]["model"] == "echo-default"
    assert r["slots"]["tts"] == "edge"
    assert r["slots"]["avatar"] == "xiaoyuanzi-local"
    assert r["slots"]["danmaku"] == "bilibili"
    assert r["slots"]["memory"] == "longterm"
    assert r["slots"]["proactive"] == "ticker"
    assert r["slots"]["mcp"] == "official-client"
    assert r["slots"]["sandbox"] == "safe-sandbox"
    assert r["slots"]["evolution"] == "day1-trace"
    # plugin_config.butler_smart.schedule_refresh_min 是 int
    assert isinstance(r["plugin_config"]["butler_smart"]["schedule_refresh_min"], int)
