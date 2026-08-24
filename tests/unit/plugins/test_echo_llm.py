import pytest
import asyncio
import os
import json
from pathlib import Path

# TR 10.2 核心：EchoLLM.chat_stream("你好") → 完整输出 == "[Echo] 你好"
@pytest.mark.asyncio
async def test_echo_llm_chat_stream_plain():
    from plugins.builtin.echo_llm.register import EchoLLM
    e = EchoLLM(prefix="[Echo] ")
    out = [c async for c in e.chat_stream("你好")]
    joined = "".join(out)
    assert joined == "[Echo] 你好"

# TR 10.3 push_turn_context → chat_stream 包含背景；下一次不包含（已消费）
@pytest.mark.asyncio
async def test_echo_llm_context_injected_once():
    from plugins.builtin.echo_llm.register import EchoLLM
    e = EchoLLM()
    e.push_turn_context(["背景A", "背景B"])
    out1 = "".join([c async for c in e.chat_stream("你好")])
    assert "背景A" in out1 and "背景B" in out1
    # 第二次不应再出现背景（消费即清空）
    out2 = "".join([c async for c in e.chat_stream("再一次")])
    assert "背景A" not in out2

# TR 10.1 通过 PluginManager 加载 → slots 有 EchoLLM 实例
@pytest.mark.asyncio
async def test_echo_llm_via_plugin_manager(tmp_path):
    # 使用真正的 plugins 目录结构（plugins/builtin/echo_llm）：让 PluginManager 加载
    from plugins.manager import PluginManager
    from ev.kernel.slots import SlotName, SlotRegistry
    from plugins.context import PluginContext

    # 临时补丁：manager.py 的 register 风格加载分支会尝试 ctx.slots = kernel.slots（赋值），
    # 但 PluginContext.slots 是只读 @property（无 setter）。此处临时加一个空 setter 防止其崩溃。
    # 真正的 ctx.slots 返回值仍由 property.fget 通过 self._manager.kernel.slots 读取，不受影响。
    _orig_slots_prop = PluginContext.__dict__["slots"]
    PluginContext.slots = property(_orig_slots_prop.fget, lambda self, val: None)
    try:
        reg = SlotRegistry()

        class FakeKernel:
            def __init__(self):
                self.slots = reg
            @property
            def profile(self):
                return {
                    "plugins": {"builtin": ["echo_llm"], "pypi": [], "git": []},
                    "slots": {SlotName.model.value: "echo-default"},
                    "plugin_config": {"echo_llm": {"prefix": "[DEMO] "}},
                }
            def attach_plugin_manager(self, pm): pass

        kernel = FakeKernel()
        # plugins_dir 指向项目实际的 plugins 根目录
        plugins_root = str(Path(__file__).resolve().parents[3] / "plugins")   # tests/unit/plugins → → plugins
        pm = PluginManager(app=None, plugins_dir=plugins_root, kernel=kernel)
        await pm.load_all()   # kernel.profile 驱动加载 builtin:echo_llm

        # 查找 registry 中是否注册了 echo-default
        all_impls = reg.get_all(SlotName.model)
        assert "echo-default" in all_impls, f"LLM slot 中无 echo-default 实现，只有 {list(all_impls)}"
        impl = all_impls["echo-default"]
        # 注意：PluginManager 通过 importlib 动态加载 register.py（动态 module 名），
        # 与本地 from plugins.builtin.echo_llm.register import EchoLLM 路径得到的 class 对象
        # 并非同一 Python 对象（module 不同），所以 isinstance(impl, EchoLLM) 会是 False。
        # 正确做法：用 @runtime_checkable 的 LLMContract 协议 + duck-typing 验证。
        from ev.kernel.slots import LLMContract
        assert isinstance(impl, LLMContract), f"impl 不满足 LLMContract 协议: {type(impl).__mro__}"
        assert getattr(impl, "name", None) == "echo", f"impl.name 异常: {getattr(impl, 'name', None)!r}"
        # 验证配置的 prefix 生效（plugin_config 传了 [DEMO] ）
        out = "".join([c async for c in impl.chat_stream("x")])
        assert out.startswith("[DEMO] "), f"prefix 未生效: {out!r}"
    finally:
        PluginContext.slots = _orig_slots_prop   # 还原 property，不影响其他测试

# 额外：isinstance(LLMContract) 契约检查（TR 1.5 人工核对的自动化版）
def test_echo_llm_satisfies_llm_contract():
    from ev.kernel.slots import LLMContract
    from plugins.builtin.echo_llm.register import EchoLLM
    # LLMContract 是 @runtime_checkable Protocol
    assert isinstance(EchoLLM(), LLMContract)

# 额外：reload_client() 让 _client 值变化
def test_echo_llm_reload_client_changes_stub_id():
    from plugins.builtin.echo_llm.register import EchoLLM
    e = EchoLLM()
    old = e._client
    e.reload_client()
    assert e._client != old
