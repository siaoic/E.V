# -*- coding: utf-8 -*-
"""观众事件准入预算（admission.py）的语义契约测试。

对齐 Cortico ``audience-admission.ts`` 的核心行为：重要观众资格、拥挤滞回、
预算车道、fair_debt 回补，以及本移植的两处刻意差异（时间窗口预算 / 债务回补）。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bilibili_kit.admission import AudienceAdmission, AudienceTuning, estimate_tokens  # noqa: E402

DAY_MS = 24 * 60 * 60 * 1000


class Clock:
    """可推进的假时钟（毫秒）。"""

    def __init__(self, start: int = 1_700_000_000_000) -> None:
        self.now_ms = start

    def __call__(self) -> int:
        return self.now_ms

    def advance(self, ms: float) -> None:
        self.now_ms += int(ms)


def make_admission(clock: Clock, **tuning) -> AudienceAdmission:
    return AudienceAdmission(room_id=1, tuning=AudienceTuning(**tuning), now=clock)


# --------------------------------------------------------------------------- #
# 估算器


def test_estimate_tokens_cjk_and_other():
    assert estimate_tokens("你好") == 2  # 2 CJK → ceil(1.2)
    assert estimate_tokens("ab") == 1  # 2 other → ceil(0.6)
    assert estimate_tokens("你好ab") == 2  # ceil(1.2 + 0.6)


# --------------------------------------------------------------------------- #
# 重要观众资格


def test_unknown_viewer_is_ordinary():
    clock = Clock()
    admission = make_admission(clock)
    assert admission.importance_of("123") == []


def test_guard_grants_importance_for_retention():
    clock = Clock()
    admission = make_admission(clock)
    admission.observe("100", guard=True)
    assert admission.importance_of("100") == ["guard"]
    clock.advance(35 * DAY_MS + 1000)
    assert admission.importance_of("100") == []


def test_superchat_single_threshold_and_rolling_window():
    clock = Clock()
    admission = make_admission(clock, superchat_single_yuan=30, superchat_rolling_yuan=50)
    admission.observe("200", superchat_yuan=29)
    assert admission.importance_of("200") == []  # 单笔不足、滚动不足
    admission.observe("200", superchat_yuan=25)
    assert "superchat" in admission.importance_of("200")  # 滚动 54 ≥ 50
    clock.advance(30 * DAY_MS + 1000)
    assert admission.importance_of("200") == []  # 租约过期


def test_interaction_requires_minutes_per_stream():
    clock = Clock()
    admission = make_admission(clock, interaction_minutes_per_stream=3)
    admission.start_stream()
    for _ in range(3):
        clock.advance(61_000)  # 跨分钟
        admission.observe("300", interaction=True)
    assert "interaction" in admission.importance_of("300")


# --------------------------------------------------------------------------- #
# 拥挤滞回与限流开关


def test_no_crowd_signal_means_passthrough():
    clock = Clock()
    admission = make_admission(clock, line_budget=1, token_budget=5)
    for _ in range(10):
        assert admission.admit("这是一条很长的弹幕内容", "999")["accepted"] is True


def test_crowd_hysteresis_and_release():
    clock = Clock()
    admission = make_admission(clock, crowded_on=200, crowded_off=170, crowd_release_ms=120_000)
    admission.observe_online_rank(300)
    assert admission._crowd_active(clock()) is True

    admission.observe_online_rank(100)  # 低于下限，但要持续 120s
    assert admission._crowd_active(clock()) is True
    clock.advance(120_000)
    admission.observe_online_rank(100)
    assert admission._crowd_active(clock()) is False


def test_stale_signal_deactivates_limiting():
    clock = Clock()
    admission = make_admission(clock, crowd_stale_hold_ms=300_000)
    admission.observe_online_rank(300)
    clock.advance(301_000)
    assert admission._crowd_active(clock()) is False


# --------------------------------------------------------------------------- #
# 窗口预算与车道


def make_crowded(clock: Clock, **tuning) -> AudienceAdmission:
    admission = make_admission(clock, **tuning)
    admission.observe_online_rank(300)
    return admission


def test_limiting_admits_within_budget_then_drops():
    clock = Clock()
    admission = make_crowded(clock, line_budget=3, token_budget=1000)
    results = [admission.admit(f"弹幕{i}", f"{900 + i}") for i in range(5)]
    assert [r["accepted"] for r in results] == [True, True, True, False, False]
    assert results[3]["dropped_reason"] == "budget_full"


def test_window_rollover_resets_budget():
    clock = Clock()
    admission = make_crowded(clock, line_budget=2, token_budget=1000, window_seconds=10)
    assert admission.admit("a1", "1")["accepted"] is True
    assert admission.admit("a2", "2")["accepted"] is True
    assert admission.admit("a3", "3")["accepted"] is False
    clock.advance(11_000)
    assert admission.admit("a4", "4")["accepted"] is True


def test_critical_sc_always_passes_and_counts():
    clock = Clock()
    admission = make_crowded(clock, line_budget=1, token_budget=1)
    # SC 是 critical：即使预算爆表也放行
    assert admission.admit("sc", "42", critical=True)["accepted"] is True
    assert admission.admit("普通弹幕", "900")["accepted"] is False


def test_important_lane_share_cap_and_debt_bypass():
    clock = Clock()
    admission = make_crowded(
        clock,
        line_budget=8,
        token_budget=1000,
        important_budget_share=0.5,  # 车道限额 = int(8*0.5) = 4 行
    )
    admission.observe("100", guard=True)

    # 普通弹幕先占 2 行总预算（普通仅受总预算约束）
    assert admission.admit("普通甲", "801")["accepted"] is True
    assert admission.admit("普通乙", "802")["accepted"] is True

    # 重要观众：车道限额 4 行内直接放行
    for i in range(4):
        result = admission.admit(f"重要第{i + 1}条", "100")
        assert result["accepted"] is True, f"第{i + 1}条应放行"
    assert admission._viewers["100"].fair_debt == -4

    # 车道满（总预算还剩 2 行）：后续重要弹幕被拒并逐条记债
    for i in range(7):
        result = admission.admit(f"重要回补前第{i + 1}条", "100")
        assert result["accepted"] is False, f"回补前第{i + 1}条应被拒"
    assert admission._viewers["100"].fair_debt == 3  # -4 + 7

    # 债务 ≥3：越过车道限额回补一条（总预算 6/8 → 7/8 仍够）→ Relief 3
    result = admission.admit("重要回补条", "100")
    assert result["accepted"] is True
    assert admission._viewers["100"].fair_debt == 0

    # 回补后车道仍满：恢复逐条记账
    assert admission.admit("重要回补后一条", "100")["accepted"] is False
    assert admission._viewers["100"].fair_debt == 1


def test_ordinary_uid_zero_is_admitted_by_budget_only():
    clock = Clock()
    admission = make_crowded(clock, line_budget=1, token_budget=1000)
    assert admission.admit("未登录观众", "0")["accepted"] is True
    assert admission.admit("第二位", "0")["accepted"] is False


# --------------------------------------------------------------------------- #
# 持久化


def test_ledger_persistence_roundtrip(tmp_path):
    clock = Clock()
    ledger_file = tmp_path / "audience-ledger.json"
    admission = make_admission(clock)
    admission.file = ledger_file
    admission.observe("500", guard=True)
    admission._dirty = True
    assert admission.flush() is True

    reloaded = AudienceAdmission(room_id=1, file=ledger_file, now=clock)
    assert "guard" in reloaded.importance_of("500")
    assert reloaded._viewers["500"].guard_expires_at > 0
