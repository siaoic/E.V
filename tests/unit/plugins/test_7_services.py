"""TR 16.1~16.7：7 个服务插件骨架：profile 驱动加载 → slot 注册成功 + Contract 满足。"""
from __future__ import annotations
import pytest
import asyncio
import sys
from pathlib import Path

try:
    import socketio  # noqa
except Exception:
    class _Stub:
        class AsyncClient: pass
    sys.modules.setdefault("socketio", _Stub())

PLUGINS_ROOT = str(Path(__file__).resolve().parents[3] / "plugins")


async def _boot(builtin_names, bindings, plugin_cfg, tmp_path):
    from plugins.manager import PluginManager
    from ev.kernel.slots import SlotRegistry, SlotName

    reg = SlotRegistry()
    class FK:
        def __init__(self):
            self.slots = reg
        @property
        def profile(self):
            b = {s.value if hasattr(s,"value") else s: n for s,n in bindings.items()}
            return {"plugins":{"builtin":list(builtin_names),"pypi":[],"git":[]},"slots":b,"plugin_config":plugin_cfg}
        def attach_plugin_manager(self, pm): pass
    pm = PluginManager(app=None, plugins_dir=PLUGINS_ROOT, kernel=FK())
    await pm.load_all()
    # activate all bindings
    for s, n in bindings.items():
        try: reg.activate(s, n)
        except Exception: pass
    return reg


@pytest.mark.asyncio
async def test_t16_1_tts_edge(tmp_path):
    from ev.kernel.slots import SlotName, TTSContract
    reg = await _boot(["tts_edge"], {SlotName.tts:"edge"}, {"tts_edge":{"impl_name":"edge","voice":"zh-CN-X"}}, tmp_path)
    impl = reg.get(SlotName.tts)
    assert impl is not None
    assert isinstance(impl, TTSContract)
    assert impl.name == "edge"

@pytest.mark.asyncio
async def test_t16_2_tts_gpt_sovits(tmp_path):
    from ev.kernel.slots import SlotName, TTSContract
    reg = await _boot(["tts_gpt_sovits"], {SlotName.tts:"gptsovits"}, {"tts_gpt_sovits":{"impl_name":"gptsovits","api_url":"http://x:9880"}}, tmp_path)
    impl = reg.get(SlotName.tts)
    assert impl is not None and isinstance(impl, TTSContract) and impl.name == "gptsovits"

@pytest.mark.asyncio
async def test_t16_3_subtitle_server(tmp_path):
    from ev.kernel.slots import SlotName
    # SlotName.ui Contract 可能只要求 name
    reg = await _boot(["subtitle_server"], {SlotName.ui:"subtitle-sse"}, {"subtitle_server":{"impl_name":"subtitle-sse","port":7860}}, tmp_path)
    impl = reg.get(SlotName.ui)
    assert impl is not None
    assert impl.name == "subtitle-sse"

@pytest.mark.asyncio
async def test_t16_4_memory_proactive_evolution(tmp_path):
    from ev.kernel.slots import SlotName
    reg = await _boot(["memory_proactive_evolution"], {
        SlotName.memory: "longterm",
        SlotName.proactive: "ticker",
        SlotName.evolution: "day1-trace",
    }, {
        "memory_proactive_evolution": {
            "impl_name_memory":"longterm","impl_name_proactive":"ticker","impl_name_evolution":"day1-trace",
            "top_k":8,"interval_sec":120,"log_dir":"./data/evo",
        }
    }, tmp_path)
    m = reg.get(SlotName.memory); p = reg.get(SlotName.proactive); e = reg.get(SlotName.evolution)
    assert m is not None and p is not None and e is not None
    assert m.name == "longterm" and p.name == "ticker" and e.name == "day1-trace"

@pytest.mark.asyncio
async def test_t16_5_mcp_sandbox(tmp_path):
    from ev.kernel.slots import SlotName
    reg = await _boot(["mcp_sandbox"], {
        SlotName.mcp: "official-client",
        SlotName.sandbox: "safe-sandbox",
    }, {
        "mcp_sandbox": {
            "impl_name_mcp":"official-client","impl_name_sandbox":"safe-sandbox",
            "servers": {"filesystem":{"command":"npx"}},"allow_network":False,"memory_limit_mb":256,
        }
    }, tmp_path)
    mcp = reg.get(SlotName.mcp); sbx = reg.get(SlotName.sandbox)
    assert mcp is not None and sbx is not None
    assert mcp.name == "official-client" and sbx.name == "safe-sandbox"

@pytest.mark.asyncio
async def test_t16_6_danmaku_filter(tmp_path):
    from ev.kernel.slots import SlotName, DanmakuContract
    reg = await _boot(["danmaku_filter"], {SlotName.danmaku: "bili-filtered"}, {
        "danmaku_filter": {"impl_name":"bili-filtered","min_length":2,"blacklist":["签到"],"allow_gifts":True}
    }, tmp_path)
    impl = reg.get(SlotName.danmaku)
    assert impl is not None and isinstance(impl, DanmakuContract)
    assert impl.name == "bili-filtered"

@pytest.mark.asyncio
async def test_t16_7_avatar_xiaoyuanzi_local(tmp_path):
    from ev.kernel.slots import SlotName, AvatarContract
    reg = await _boot(["avatar_xiaoyuanzi_local"], {SlotName.avatar:"xiaoyuanzi-local"}, {
        "avatar_xiaoyuanzi_local": {"impl_name":"xiaoyuanzi-local","host":"127.0.0.1","port":8765,"exe_path":""}
    }, tmp_path)
    impl = reg.get(SlotName.avatar)
    assert impl is not None and isinstance(impl, AvatarContract)
    assert impl.name == "xiaoyuanzi-local"
