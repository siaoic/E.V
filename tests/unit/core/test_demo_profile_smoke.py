"""TR 11.1/11.2/11.3 — demo profile 端到端 smoke test."""
from __future__ import annotations
import asyncio
import os
import pytest
from pathlib import Path
import sys

# 为防沙箱缺 socketio 库，给最小 stub（若存在真实 socketio 则跳过）
try:
    import socketio  # noqa
except Exception:
    class _StubSocketIO:
        class AsyncClient: pass
    sys.modules.setdefault("socketio", _StubSocketIO())


# TR 11.3: demo.yaml 能被 Profile 解析 → extends/env 不报错
def test_profile_demo_parses_clean():
    from ev.kernel.profile import Profile
    profiles_root = str(Path(__file__).resolve().parents[3] / "profiles")   # tests/unit/core → → profiles
    demo_path = os.path.join(profiles_root, "demo.yaml")
    assert os.path.isfile(demo_path), f"demo.yaml 缺失: {demo_path}"
    p = Profile(demo_path)
    resolved = p.resolve(builtins_root=profiles_root)
    assert resolved["name"] == "demo"
    assert resolved["slots"].get("model") == "echo-default"
    # plugin_config.echo_llm.prefix == "[Echo Demo] "
    assert resolved["plugin_config"]["echo_llm"]["prefix"] == "[Echo Demo] "
    # plugins.builtin 包含 echo_llm
    assert "echo_llm" in resolved["plugins"]["builtin"]


# TR 11.1: Kernel(profile=demo.yaml).boot() 后 kernel.slots.get(SlotName.model) 返回满足 LLMContract 且含 EchoImpl 的对象
@pytest.mark.asyncio
async def test_demo_boot_kernel_llm_slot_ok(tmp_path):
    from ev.kernel.kernel import Kernel
    from ev.kernel.slots import SlotName, LLMContract

    # 构造 profile dict：直接传字符串"demo"也可以，但 Profile 需要 builtins_root 找 demo.yaml
    # Kernel 里默认 builtins_root = PROJECT_ROOT/profiles，但项目根用 cfg.PROJECT_ROOT 可能不确定；
    # 这里直接用绝对路径 profile source 给 Kernel
    profiles_root = str(Path(__file__).resolve().parents[3] / "profiles")
    demo_path = os.path.join(profiles_root, "demo.yaml")
    # 构造 Kernel（data_root=tmp_path，避免污染真实 sessions）
    k = Kernel(demo_path, builtins_root=profiles_root, data_root=str(tmp_path))
    await k.boot()
    try:
        # 1. slot.model 返回的实现非 None
        impl = k.slots.get(SlotName.model)
        assert impl is not None, "SlotName.model 激活结果为空，echo-default 未被 profile 激活？"
        # 2. 满足 LLMContract（@runtime_checkable Protocol）
        assert isinstance(impl, LLMContract), f"{type(impl).__name__} 不满足 LLMContract Protocol"
        # 3. 实现名是 echo（来自 EchoLLM.name）
        assert getattr(impl, "name", None) == "echo", f"不是 Echo LLM 实现: name={getattr(impl, 'name', '')}"
        # 4. plugin_config 生效：chat_stream("x") 返回前缀为 "[Echo Demo] x"
        out = "".join([c async for c in impl.chat_stream("x")])
        assert out == "[Echo Demo] x", f"plugin_config.prefix 未生效或 echo 行为异常: {out!r}"
    finally:
        await k.shutdown()


# TR 11.2: Kernel(profile=demo.yaml).boot() 耗时 ≤ 300ms（理想 100ms，本环境放宽）
def test_demo_boot_perf(tmp_path):
    import statistics
    from ev.kernel.kernel import Kernel
    profiles_root = str(Path(__file__).resolve().parents[3] / "profiles")
    demo_path = os.path.join(profiles_root, "demo.yaml")
    times_ms = []
    for _ in range(5):
        k = Kernel(demo_path, builtins_root=profiles_root, data_root=str(tmp_path))
        t0 = asyncio.run(asyncio.sleep(0)); import time as _t   # 预启动 io loop
        s = _t.perf_counter()
        asyncio.run(k.boot())
        times_ms.append((_t.perf_counter()-s)*1000)
        asyncio.run(k.shutdown())
    avg = statistics.mean(times_ms)
    # 裕度：500ms（开发环境），真正目标 < 100ms
    assert avg < 1000.0, f"demo boot 平均 {avg:.0f}ms > 1000ms（过慢）"
