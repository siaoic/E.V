"""TR 14.1 Avatar / TR 14.2 Input / TR 14.3 Danmaku：三骨架插件能成功注册 slot，最小契约满足。"""
from __future__ import annotations
import pytest
import asyncio
from pathlib import Path
import sys

# socketio stub
try:
    import socketio  # noqa
except Exception:
    class _Stub:
        class AsyncClient: pass
    sys.modules.setdefault("socketio", _Stub())


PLUGINS_ROOT = str(Path(__file__).resolve().parents[3] / "plugins")


async def _boot_with_profile(builtin_names, slot_bindings, plugin_config, tmp_path):
    from plugins.manager import PluginManager
    from ev.kernel.slots import SlotRegistry, SlotName
    from plugins.context import PluginContext

    # 参考 test_echo_llm.py：PluginContext.slots 是只读 property，加空 setter 防崩溃
    _orig_slots_prop = PluginContext.__dict__["slots"]
    PluginContext.slots = property(_orig_slots_prop.fget, lambda self, val: None)
    try:
        reg = SlotRegistry()

        class FakeKernel:
            def __init__(self):
                self.slots = reg

            @property
            def profile(self):
                slots_bind = {}
                for s, n in slot_bindings.items():
                    # 兼容 SlotName 枚举 或 字符串
                    key = s.value if hasattr(s, "value") else s
                    slots_bind[key] = n
                return {
                    "plugins": {"builtin": list(builtin_names), "pypi": [], "git": []},
                    "slots": slots_bind,
                    "plugin_config": plugin_config,
                }

            def attach_plugin_manager(self, pm):
                pass

        kernel = FakeKernel()
        pm = PluginManager(app=None, plugins_dir=PLUGINS_ROOT, kernel=kernel)
        await pm.load_all()

        # 加载后激活：按 slot_bindings 把 impl 激活，让 reg.get() 返回值
        for s, impl_name in slot_bindings.items():
            slot = s if isinstance(s, SlotName) else SlotName(s)
            # 确保该 impl 已注册（插件 register 调用过了）
            all_impls = reg.get_all(slot)
            if impl_name in all_impls:
                reg.activate(slot, impl_name)

        return reg
    finally:
        PluginContext.slots = _orig_slots_prop   # 还原 property，不影响其他测试


# --- TR 14.1 Avatar ---
@pytest.mark.asyncio
async def test_avatar_skeleton_registered(tmp_path):
    from ev.kernel.slots import SlotName, AvatarContract
    reg = await _boot_with_profile(
        ["avatar_xiaoyuanzi_remote"],
        {SlotName.avatar: "xiaoyuanzi"},
        {"avatar_xiaoyuanzi_remote": {"endpoint": "http://fake:9999", "impl_name": "xiaoyuanzi"}},
        tmp_path,
    )
    impl = reg.get(SlotName.avatar)
    assert impl is not None and impl.name == "xiaoyuanzi"
    # 满足 AvatarContract
    assert isinstance(impl, AvatarContract)
    # 生命周期：start → stop 不崩
    await impl.start()
    assert impl.running is True
    await impl.set_emotion("happy", 1)
    await impl.stop()
    assert impl.running is False


# --- TR 14.2 Input ---
@pytest.mark.asyncio
async def test_input_skeleton_registered(tmp_path):
    from ev.kernel.slots import SlotName, InputContract
    reg = await _boot_with_profile(
        ["input_cli"],
        {SlotName.input: "cli"},
        {"input_cli": {"impl_name": "cli", "prompt": "[TEST] "}},
        tmp_path,
    )
    impl = reg.get(SlotName.input)
    assert impl is not None and impl.name == "cli"
    assert isinstance(impl, InputContract)
    # start → 迭代一次立即结束（骨架 stub 行为）
    await impl.start()
    msgs = [x async for x in impl]
    assert msgs == []
    await impl.stop()


# --- TR 14.3 Danmaku ---
@pytest.mark.asyncio
async def test_danmaku_skeleton_registered(tmp_path):
    from ev.kernel.slots import SlotName, DanmakuContract
    reg = await _boot_with_profile(
        ["danmaku_bilibili"],
        {SlotName.danmaku: "bilibili"},
        {"danmaku_bilibili": {"impl_name": "bilibili", "room_id": 123456}},
        tmp_path,
    )
    impl = reg.get(SlotName.danmaku)
    assert impl is not None and impl.name == "bilibili"
    assert isinstance(impl, DanmakuContract)
    assert impl.room_id == 123456
    # on_message 注册 → emit 假消息 → handler 被调
    received = []

    async def _h(m):
        received.append(m)

    impl.on_message(_h)
    await impl.connect()
    assert impl.running is True
    await impl._emit({"user": "u", "text": "hi", "type": "danmaku"})
    await impl.disconnect()
    assert received == [{"user": "u", "text": "hi", "type": "danmaku"}]
