"""主动对话重构（Neuro 风格契机驱动）回归测试。

覆盖：
  - NudgeEngine：5 种契机 / 冷却 / 重复抑制 / 窗口上限 / 优先级 / 统计
  - ProactiveEngine：契机门控心跳 / 强制兜底 / request_speak / 标记解析 /
    互动结束氛围切换 / TTL 过期 / 回退模式

运行：python -m pytest tests/test_proactive_nudge.py -q
"""

import asyncio
import time

import pytest

from ev.llm.proactive import ProactiveEngine
from ev.llm.proactive.nudge import (
    NudgeEngine,
    NudgeEvent,
    NudgeReason,
    reset_engine,
)
from ev.llm.proactive.policies import _pick_topic
from ev.utils import config


# ---------- fakes ----------

class FakeBrain:
    def __init__(self, reply="这首歌的间奏比我家猫的作息还随机"):
        self.history = []
        self._reply = reply

    async def chat_stream(self, prompt, proactive=False):
        yield ("final", self._reply)


class SilentBrain(FakeBrain):
    async def chat_stream(self, prompt, proactive=False):
        yield ("final", "[SILENT]")


class FakeSub:
    def push(self, *a, **k):
        pass


def make_cfg(**over):
    c = config.cfg
    c.PROACTIVE_ENABLED = True
    c.AGENT_AVOID_MAIN_LLM = False
    c.AGENT_DUP_THRESHOLD = 0.85
    c.AGENT_HISTORY_SNAPSHOT = 6
    c.PROACTIVE_QUEUE_MAX = 4
    c.PROACTIVE_NUDGE_ENABLED = True
    c.PROACTIVE_FORCE_SPEAK = True
    for k, v in over.items():
        setattr(c, k, v)
    return c


def make_engine(brain_cls=FakeBrain, **over):
    reset_engine()
    return ProactiveEngine(
        brain_cls(), None, None, FakeSub(), make_cfg(**over))


# ---------- NudgeEngine 单元 ----------

def test_long_silence_nudge_and_listener():
    eng = NudgeEngine()
    fired = []
    eng.add_listener(fired.append)
    eng.last_activity_ts = time.time() - 40
    n = eng.check()
    assert n is not None and n.reason == NudgeReason.LONG_SILENCE
    assert fired == [n]
    assert "安静" in n.prompt_hint


def test_same_reason_repeat_suppression():
    eng = NudgeEngine()
    eng.last_activity_ts = time.time() - 40
    assert eng.check() is not None
    assert eng.check() is None  # repeat_gap 内不重复推


def test_burst_nudge_fires_on_threshold():
    eng = NudgeEngine(burst_threshold=5, burst_window_sec=30.0)
    n = None
    for i in range(5):
        n = eng.observe("danmaku", {"text": f"m{i}"}) or n
    assert n is not None and n.reason == NudgeReason.BURST
    assert eng.observe("danmaku") is None  # 冷却内不再推


def test_many_unread_and_ai_spoke_reset():
    eng = NudgeEngine(many_unread_threshold=5, burst_threshold=999)
    n = None
    for _ in range(5):
        n = eng.observe("danmaku") or n
    assert n is not None and n.reason == NudgeReason.MANY_UNREAD
    eng.observe("ai_spoke")
    assert eng.unread_count == 0


def test_silent_too_long_is_forcible():
    eng = NudgeEngine()
    eng.last_ai_speak_ts = time.time() - 400
    eng.last_nudge_ts = 0
    n = eng.check()
    assert n is not None and n.reason == NudgeReason.SILENT_TOO_LONG
    assert eng.is_forcible(n.reason)


def test_state_change_not_forcible_and_freshness():
    eng = NudgeEngine()
    n = eng.observe("state_change", {"from": "interactive", "to": "idle"})
    assert n is not None and n.reason == NudgeReason.STATE_CHANGE
    assert not eng.is_forcible(n.reason)
    eng.last_state_change_ts = time.time() - 60
    eng.last_nudge_ts = 0
    assert eng.check() is None  # 新鲜期（30s）已过


def test_user_input_clears_unread():
    eng = NudgeEngine()
    for _ in range(8):
        eng.observe("danmaku")
    eng.observe("user_input")
    assert eng.unread_count == 0


def test_window_cap_limits_per_minute():
    eng = NudgeEngine(window_cap=3, nudge_cooldown_sec=0.0, repeat_gap_sec=0.0)
    eng.last_activity_ts = 0  # long_silence 恒可触发
    count = 0
    for _ in range(6):
        eng.last_nudge_ts = 0
        if eng.check() is not None:
            count += 1
    assert count == 3


def test_priority_state_change_first():
    eng = NudgeEngine(window_cap=99, repeat_gap_sec=0.0)
    eng.observe("danmaku")
    eng.observe("danmaku")
    eng.observe("state_change", {"from": "a", "to": "b"})
    eng.last_nudge_ts = 0
    n = eng.check()
    assert n is not None and n.reason == NudgeReason.STATE_CHANGE


def test_stats_track_act_reject():
    eng = NudgeEngine()
    eng.report_act()
    eng.report_reject()
    s = eng.get_stats()
    assert s["nudge_acted"] == 1 and s["nudge_rejected"] == 1


# ---------- ProactiveEngine 集成 ----------

@pytest.mark.asyncio
async def test_heartbeat_without_nudge_is_silent_and_cheap():
    eng = make_engine()
    assert await eng.heartbeat() is False
    assert eng._stats["silent"] == 0  # 没问 LLM


@pytest.mark.asyncio
async def test_heartbeat_with_cold_field_speaks():
    eng = make_engine()
    eng.last_interaction = time.time() - 40
    eng.nudge.last_activity_ts = time.time() - 40
    assert await eng.heartbeat() is True
    assert eng.nudge.get_stats()["nudge_acted"] == 1
    await asyncio.sleep(0.15)
    assert eng._stats["speak"] == 1  # worker 播报完成（含 ai_spoke 回报）


@pytest.mark.asyncio
async def test_heartbeat_silent_reject_reports():
    eng = make_engine(SilentBrain)
    eng.last_interaction = time.time() - 15  # <25s 不触发强制兜底
    eng.nudge.last_activity_ts = time.time() - 40
    assert await eng.heartbeat() is False
    assert eng._stats["silent"] == 1
    assert eng.nudge.get_stats()["nudge_rejected"] == 1


@pytest.mark.asyncio
async def test_force_speak_fallback_overrides_silent():
    eng = make_engine(SilentBrain)
    eng.last_interaction = time.time() - 40
    eng.nudge.last_activity_ts = time.time() - 40
    assert await eng.heartbeat() is True  # 兜底以话题强制开口


@pytest.mark.asyncio
async def test_force_speak_disabled_respects_silent():
    eng = make_engine(SilentBrain, PROACTIVE_FORCE_SPEAK=False)
    eng.last_interaction = time.time() - 40
    eng.nudge.last_activity_ts = time.time() - 40
    assert await eng.heartbeat() is False
    assert eng._stats["silent"] == 1


@pytest.mark.asyncio
async def test_heartbeat_fallback_when_nudge_disabled():
    eng = make_engine(SilentBrain, PROACTIVE_NUDGE_ENABLED=False)
    assert await eng.heartbeat() is False  # 旧行为：直问 LLM → 拒
    assert eng._stats["silent"] == 1


@pytest.mark.asyncio
async def test_request_speak_paths():
    eng = make_engine()
    res = await eng.request_speak(
        topic_hint="樱花与编程语言的相似性", reason="想到个梗")
    assert res["ok"] is True and res["topic"] == "樱花与编程语言的相似性"

    eng_silent = make_engine(SilentBrain)
    res = await eng_silent.request_speak(reason="随便聊聊")
    assert res["ok"] is False and res["reason"] == "silent"

    eng.speaking = True
    res = await eng.request_speak()
    assert res["ok"] is False and res["reason"] == "busy"
    eng.speaking = False


def test_parse_decision_markers():
    P = ProactiveEngine._parse_decision
    assert P("[SILENT]") is None
    assert P("<SILENT>") is None
    assert P("沉默") is None
    assert P("算了") is None
    assert P("大家好呀[END]") == "大家好呀"
    assert P("[END]") is None
    assert P("「今天的风儿甚是喧嚣」") == "「今天的风儿甚是喧嚣」"


@pytest.mark.asyncio
async def test_interaction_end_state_change_nudge():
    eng = make_engine()
    eng.on_ai_spoke()  # 互动回复完成 → 氛围切换契机
    assert await eng.heartbeat() is True  # 心跳消费契机 → 开口机会


def test_get_stats_aggregates_nudge():
    eng = make_engine()
    s = eng.get_stats()
    for k in ("trigger", "speak", "silent", "dropped", "nudge_total",
              "nudge_acted", "nudge_rejected", "queue_size"):
        assert k in s


@pytest.mark.asyncio
async def test_stale_pending_nudge_discarded():
    eng = make_engine()
    eng._pending_nudge = NudgeEvent(
        NudgeReason.BURST, ts=time.time() - 60, prompt_hint="过期")
    assert await eng.heartbeat() is False
    assert eng._pending_nudge is None


def test_empty_topic_pool_safe():
    eng = make_engine()
    eng._topic_seeds = []
    assert _pick_topic(eng) is None


@pytest.mark.asyncio
async def test_danmaku_pipeline_full_chain():
    """弹幕堆积 → observe → listener 暂存 + 唤醒 → 心跳开口。"""
    from ev.llm.proactive.nudge import observe as nudge_observe
    eng = make_engine()
    for i in range(5):
        nudge_observe("danmaku", {"text": f"m{i}"})
    assert eng._pending_nudge is not None
    assert eng._wakeup.is_set()
    assert await eng.heartbeat() is True
