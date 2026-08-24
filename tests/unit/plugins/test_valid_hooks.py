"""Task 6 单元测试：VALID_HOOKS 17 个、Plugin 17 个空钩子、3 个新事件类。"""

import asyncio
import pytest

from plugins.base import (
    VALID_HOOKS,
    Plugin,
    SlotActivateEvent,
    DanmakuEvent,
    EmotionDecideEvent,
)


# --- TR 6.1: VALID_HOOKS 数量与内容 ---

OLD_HOOKS = [
    "on_init", "on_start", "on_stop", "on_destroy",
    "on_user_input", "on_llm_request", "on_llm_response",
    "on_tts_text", "on_tts_start", "on_tts_end",
]

NEW_HOOKS = [
    "on_slot_activate",
    "on_session_start", "on_session_end",
    "on_danmaku",
    "on_emotion_decide", "on_proactive_decide",
    "on_config_reload",
]


def test_valid_hooks_count():
    """TR 6.1: VALID_HOOKS 必须恰好 17 个。"""
    assert len(VALID_HOOKS) == 17


@pytest.mark.parametrize("hook", OLD_HOOKS)
def test_old_hooks_present(hook):
    """TR 6.1: 10 个旧钩子全部在 VALID_HOOKS 中。"""
    assert hook in VALID_HOOKS


@pytest.mark.parametrize("hook", NEW_HOOKS)
def test_new_hooks_present(hook):
    """TR 6.1: 7 个新钩子全部在 VALID_HOOKS 中。"""
    assert hook in VALID_HOOKS


# --- TR 6.2: Plugin 17 个钩子均可调用且空实现不抛异常 ---

ALL_HOOKS = OLD_HOOKS + NEW_HOOKS


def _build_hook_args(hook_name: str) -> tuple:
    """根据钩子名返回调用时的占位参数。"""
    slot_evt = SlotActivateEvent(slot_name="test_slot", old_impl=None, new_impl=object())
    danmaku_evt = DanmakuEvent(item={
        "user_name": "alice",
        "user_id": "1001",
        "content": "hello",
        "source": "bilibili",
        "room_id": "12345",
        "timestamp": 1700000000,
    })
    emotion_evt = EmotionDecideEvent(
        text="今天好开心",
        emotion_candidates=[("happy", 0.9), ("neutral", 0.05)],
        decided=None,
    )
    # 原 10 个钩子
    if hook_name in ("on_init", "on_start", "on_stop", "on_destroy",
                     "on_session_start", "on_session_end", "on_tts_end"):
        return ()
    if hook_name == "on_user_input":
        return (None,)
    if hook_name == "on_llm_request":
        return (None,)
    if hook_name == "on_llm_response":
        return (None,)
    if hook_name == "on_tts_text":
        return ("sample text",)
    if hook_name == "on_tts_start":
        return ("sample text",)
    # 新增 7 个钩子
    if hook_name == "on_slot_activate":
        return (slot_evt,)
    if hook_name == "on_danmaku":
        return (danmaku_evt,)
    if hook_name == "on_emotion_decide":
        return (emotion_evt,)
    if hook_name == "on_proactive_decide":
        return (None,)
    if hook_name == "on_config_reload":
        return ({},)
    return ()


@pytest.mark.parametrize("hook_name", ALL_HOOKS)
def test_plugin_hooks_callable_no_raise(hook_name):
    """TR 6.2: 17 个钩子全部可 getattr 并异步调用，空实现不抛异常。"""
    p = Plugin()
    method = getattr(p, hook_name, None)
    assert method is not None, f"Plugin 缺少钩子方法：{hook_name}"
    args = _build_hook_args(hook_name)
    # on_tts_text 返回 text，其他返回 None；空实现都不应抛异常
    result = asyncio.run(method(*args))
    if hook_name == "on_tts_text":
        assert result == "sample text"
    else:
        assert result is None


# --- TR 6.3: 3 个新事件类构造与属性访问 ---

def test_slot_activate_event():
    """TR 6.3: SlotActivateEvent 构造与属性。"""
    old_obj = object()
    new_obj = object()
    evt = SlotActivateEvent(slot_name="my_slot", old_impl=old_obj, new_impl=new_obj)
    assert evt.slot_name == "my_slot"
    assert evt.old_impl is old_obj
    assert evt.new_impl is new_obj


def test_danmaku_event():
    """TR 6.3: DanmakuEvent 构造、便捷属性与缺失字段兜底。"""
    item = {
        "user_name": "bob",
        "user_id": "2002",
        "content": "666",
        "source": "douyin",
        "room_id": "777",
        "timestamp": 1700000123,
    }
    evt = DanmakuEvent(item=item)
    assert evt.item is item
    assert evt.user_name == "bob"
    assert evt.content == "666"
    assert evt.source == "douyin"

    # 缺失字段兜底空串
    evt2 = DanmakuEvent(item={})
    assert evt2.user_name == ""
    assert evt2.content == ""
    assert evt2.source == ""


def test_emotion_decide_event():
    """TR 6.3: EmotionDecideEvent 构造 + decided 可修改。"""
    candidates = [("happy", 0.85), ("neutral", 0.10), ("sad", 0.05)]
    evt = EmotionDecideEvent(text="I am fine", emotion_candidates=candidates)
    assert evt.text == "I am fine"
    assert evt.emotion_candidates is candidates
    assert evt.decided is None

    # decided 默认参数
    evt2 = EmotionDecideEvent(
        text="哈哈",
        emotion_candidates=[("joy", 0.9)],
        decided="joy",
    )
    assert evt2.decided == "joy"

    # 插件钩子可修改 decided
    evt.decided = "neutral"
    assert evt.decided == "neutral"
