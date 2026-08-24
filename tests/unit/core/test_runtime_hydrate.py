"""TR 9.1 ~ 9.4: RuntimeContext 的 kernel 参数 / _hydrate_from_slots() / setup kernel 分支。

只断言关键路径行为，不真正走 4.x setup（太重）。
"""

from __future__ import annotations

import sys
import types

import pytest

# 由于本环境未安装 python-socketio（runtime.py 会经过 src.mindcraft.bridge → socketio），
# 且 socketio 不是本任务断言核心，在此注入一个最小替身，确保 RuntimeContext 能 import。
# 这与 conftest 的 sys.path 补齐策略一致：保证 import 成功，后续断言不依赖 mindcraft。
if "socketio" not in sys.modules:
    _socketio_stub = types.ModuleType("socketio")
    _socketio_stub.AsyncClient = object  # type: ignore[attr-defined]
    sys.modules["socketio"] = _socketio_stub

from ev.kernel.runtime import RuntimeContext


# TR 9.1 & 9.3: kernel=None 路径，__init__ 字段初始化 ≡ 原来的行为
def test_runtime_init_no_kernel_has_attrs():
    class DummyCfg:
        RUN_MODE = "live"
        PROJECT_ROOT = "."
        LLM_MODEL = "dummy"
        LLM_THINKING = False
        PET_MODEL_PATH = None
        EMOTION_ACTOR_ENABLED = False
        VTS_PORT = 8001
        PET_MOTION_PATH = None
        MEMORY_ENABLED = False
        EVOLUTION_ENABLED = False
        MCP_ENABLED = False
        TOOLS_ENABLED = False
        PROFANITY_FILTER_ENABLED = False
        GPTSOVITS_REF_AUDIO = None
        MINDCRAFT_BRIDGE_ENABLED = False
        PROACTIVE_ENABLED = False
        STT_ENABLED = False
        BILI_ROOM_ID = None
        MOTION_PATH = None
        VTS_IDLE_TAKEOVER = False
        MOUTH_PARAMETER = None
        AGENT_ENABLED = False
        AGENT_DELEGATE_BACKEND = False

    cfg = DummyCfg()
    rt = RuntimeContext(cfg, kernel=None)

    # cfg / kernel 引用正确
    assert rt.cfg is cfg
    assert rt.kernel is None

    # __init__ 初始化的字段全部存在（baseline）
    baseline_attrs = [
        "pet_widget", "vts", "face", "emotion_actor", "sub",
        "butler", "evolution", "mcp", "brain", "proactive",
        "tts", "stt_engine", "mindcraft_bridge", "pf", "mm",
        "agent_scheduler", "danmaku_picker", "bili_svc",
        "danmaku_reply_task", "plugin_manager", "_input_source",
        "_pending_stdin_fut", "_loop", "_reloaders_map",
        "_hydrated_from_slots",
    ]
    for attr in baseline_attrs:
        assert hasattr(rt, attr), f"缺少属性 {attr}"

    # _input_source 有默认值 "text"，其余业务字段初始为 None
    assert rt._input_source == "text"
    for attr in [
        "pet_widget", "vts", "face", "emotion_actor", "sub",
        "butler", "evolution", "mcp", "brain", "proactive",
        "tts", "stt_engine", "mindcraft_bridge", "pf", "mm",
        "agent_scheduler", "danmaku_picker", "bili_svc",
        "danmaku_reply_task",
    ]:
        assert getattr(rt, attr) is None, f"attr {attr} 初始应为 None"

    # 幂等标记初始 False
    assert rt._hydrated_from_slots is False


# TR 9.2 + 9.4: kernel 有 slots → _hydrate_from_slots 后各字段指向同一对象；
# setup 调用 kernel.boot 恰好一次，且执行完就 early-return（不跑 4.x 初始化）。
@pytest.mark.asyncio
async def test_runtime_hydrate_from_slots_and_boot_called(tmp_path):
    from ev.kernel.slots import SlotName

    class FakeBrain:
        pass

    class FakeTTS:
        pass

    class FakeVTS:
        pass

    class FakeMemory:
        pass

    class FakeSlots:
        def __init__(self):
            self._store: dict = {}

        def register_enum(self, enum, impl):
            self._store[enum] = impl

        def get(self, enum):
            return self._store.get(enum)

    fs = FakeSlots()
    brain = FakeBrain()
    tts = FakeTTS()
    vts = FakeVTS()
    mm = FakeMemory()
    fs.register_enum(SlotName.model, brain)
    fs.register_enum(SlotName.tts, tts)
    fs.register_enum(SlotName.avatar, vts)
    fs.register_enum(SlotName.memory, mm)

    boot_calls: list[int] = []

    class FakeKernel:
        def __init__(self):
            self.slots = fs
            self.plugin_manager = None
            self.attached_pms: list = []

        def attach_plugin_manager(self, pm):
            self.attached_pms.append(pm)

        async def boot(self):
            boot_calls.append(1)

    kernel = FakeKernel()

    # Dummy cfg：kernel 路径下 setup 不会触达 4.x 初始化，所以随便返回 None 即可
    class Cfg:
        RUN_MODE = "live"
        PROJECT_ROOT = str(tmp_path)

        def __getattr__(self, name):
            return None

    rt = RuntimeContext(Cfg(), kernel=kernel)
    assert rt.kernel is kernel

    # 1) 首次 hydrate：brain/tts/vts/mm 应该就是 slots 中注册的同一对象
    await rt._hydrate_from_slots()
    assert rt.brain is brain
    assert rt.tts is tts
    assert rt.vts is vts
    assert rt.mm is mm
    # 幂等：已 True
    assert rt._hydrated_from_slots is True

    # 2) 再次无 force 调用：不应变（验证 _hydrated_from_slots 守卫有效）
    new_brain = FakeBrain()
    fs.register_enum(SlotName.model, new_brain)
    await rt._hydrate_from_slots()
    assert rt.brain is brain, "未传 force=True 时不应重复 hydrate"

    # 3) force=True：允许刷新
    await rt._hydrate_from_slots(force=True)
    assert rt.brain is new_brain, "force=True 时应允许重新 hydrate"

    # 4) setup()：kernel 分支应执行 boot() 恰好一次，然后 early-return
    # 复位计数
    boot_calls.clear()
    # 复位 hydrate 标记，模拟 setup 时的真实初始状态
    rt._hydrated_from_slots = False
    # 复位 brain 为 None 验证 setup 内 hydrate 会把它重新设为 slots 里的值
    fs.register_enum(SlotName.model, brain)
    rt.brain = None

    await rt.setup()
    assert len(boot_calls) == 1, (
        f"kernel.boot() 被调用了 {len(boot_calls)} 次 ≠ 1"
    )
    # setup 的 kernel 路径应在末尾赋值 _loop，并在 cfg=self.cfg 之前 return，
    # 因此 4.x 的大量组件初始化完全不执行，brain 来自 hydrate 而非 LLMBrain
    assert rt._loop is not None, "kernel 路径应设置 self._loop"
    assert rt.brain is brain, (
        "setup 中 hydrate + boot 后 brain 应等于 slots 注册的实现"
    )
