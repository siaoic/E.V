"""SlotRegistry 与 SlotName 的单元测试。"""

import pytest

from ev.kernel.slots import (
    AvatarContract,
    InputContract,
    LLMContract,
    SlotName,
    SlotRegistry,
    TTSContract,
)


# TR 1.1: SlotName 枚举长度与值集合校验（spec 标称 16，但清单包含 17 项，以清单为准）
_EXPECTED_VALUES = {
    "model", "tts", "avatar", "input", "danmaku", "memory", "emotion",
    "proactive", "mcp", "butler", "evolution", "scheduler", "sandbox",
    "ui", "pet", "session", "credentials",
}
_EXPECTED_LEN = len(_EXPECTED_VALUES)  # 17


def test_slot_enum_complete():
    """TR 1.1: 枚举完整覆盖所有约定的字符串值且数量一致。"""
    actual_values = {s.value for s in SlotName}
    assert len(list(SlotName)) == _EXPECTED_LEN
    assert _EXPECTED_VALUES <= actual_values


# TR 1.2: 注册 -> 激活 -> 切换 -> 取回
def test_register_activate_get():
    """TR 1.2: 同 slot 注册多个实现，按 activate 正确切换 get。"""
    reg = SlotRegistry()
    objA = object()
    objB = object()

    reg.register(SlotName.model, "impl-a", objA)
    reg.register(SlotName.model, "impl-b", objB)

    reg.activate(SlotName.model, "impl-a")
    assert reg.get(SlotName.model) is objA

    reg.activate(SlotName.model, "impl-b")
    assert reg.get(SlotName.model) is objB


# TR 1.3: 同 slot 同 impl_name 二次注册抛 ValueError
def test_register_conflict():
    """TR 1.3: 冲突注册必须抛 ValueError。"""
    reg = SlotRegistry()
    reg.register(SlotName.tts, "echo", object())
    with pytest.raises(ValueError):
        reg.register(SlotName.tts, "echo", object())


# TR 1.4: 切换时先旧 on_deactivate 再新 on_activate，顺序严格
class _HookedImpl:
    """带激活/停用钩子的 mock 实现。"""

    def __init__(self, tag: str, order: list[str]):
        self._tag = tag
        self._order = order

    def on_activate(self):
        self._order.append(f"{self._tag}_activate")

    def on_deactivate(self):
        self._order.append(f"{self._tag}_deactivate")


def test_activate_hooks_order():
    """TR 1.4: 从 old 切到 new 时钩子顺序为 old_deactivate -> new_activate。"""
    reg = SlotRegistry()
    order: list[str] = []
    old = _HookedImpl("old", order)
    new = _HookedImpl("new", order)

    reg.register(SlotName.avatar, "old", old)
    reg.register(SlotName.avatar, "new", new)

    reg.activate(SlotName.avatar, "old")
    order.clear()  # 只测量第二次切换的顺序

    reg.activate(SlotName.avatar, "new")
    assert order == ["old_deactivate", "new_activate"]
