"""RuntimeContext 骨架：__init__ / _hydrate / setup / teardown（精简 ≤ 400 行）。

- 5.0 kernel 分支（self.kernel is not None）一字不动；
- 4.x 旧路径改为依次调用 ev.kernel.components.*.setup() / .teardown()；
- helper 方法（弹幕/记忆/TTS/热重载 等）通过 RuntimeHelpersMixin 提供，
  保持类上方法名不变，调用方完全无感。
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Optional

from ev.utils import console

# 骨架本身只负责把这些符号挂到类/类型提示上；组件实际 import 发生在
# components/*.setup()/teardown() 内部，这样即使某可选依赖缺失（如 mindcraft
# 的 socketio），也只有到真正 setup 对应 component 才会报缺包 —— 与拆分前
# runtime.py 顶层大量 import 后被人 import RuntimeContext 即失败的行为
# 一致：旧文件也是顶层 import，失败是原有环境问题，不属本次拆分引入。
#
# 为使纯 import RuntimeContext 能过（测试的 smoke / hydrate 仅实例化、不
# 走 mindcraft setup），下面把仅用于类型注解（TYPE_CHECKING 时）的模块
# 改为 annotation-only 导入。
from tools.memory import memory  # noqa: F401  （供 import src.core.runtime.memory 兼容）
from ev.kernel.output_lock import (
    STATE_AI_SPEAKING, STATE_IDLE, get_output_lock, get_output_owner,
    set_danmaku_pending, set_output_owner, set_global_state,
)

from ev.kernel._helpers import (
    RuntimeHelpersMixin,
    _MEMORY_INTEGRATE_INTERVAL,
    _MEMORY_INTEGRATE_THRESHOLD,
)

if TYPE_CHECKING:
    from ev.llm.butler_agent import ButlerAgent
    from ev.llm.evolution import EvolutionEngine
    from ev.mcp.manager import MCPManager
    from ev.llm.proactive import ProactiveEngine
    from ev.vts.face_driver import FaceDriver
    from ev.llm.llm_brain import LLMBrain
    from ev.tts.engine import TTSEngine
    from ev.vts.controller import VTSController
    from ev.mindcraft.bridge import MindcraftBridge
    from ev.utils.subtitle_server import SubtitleServer
    from plugins import PluginManager
    from ev.utils.content_filter import ProfanityFilter


class RuntimeContext(RuntimeHelpersMixin):
    """E.V 运行时所有组件的容器与共享能力（取代 Application 的组件持有职责）。"""

    def __init__(self, cfg, kernel=None) -> None:
        self.cfg = cfg
        self.kernel = kernel
        # === 渲染/驱动目标 ===
        self.pet_widget = None
        self.vts: Optional[VTSController] = None
        self.face: Optional[FaceDriver] = None
        self.emotion_actor = None
        self.sub = None
        # === 核心组件 ===
        self.butler: Optional[ButlerAgent] = None
        self.evolution: Optional[EvolutionEngine] = None
        self.mcp: Optional[MCPManager] = None
        self.brain: Optional[LLMBrain] = None
        self.proactive: Optional[ProactiveEngine] = None
        self.tts: Optional[TTSEngine] = None
        self.stt_engine = None
        self.mindcraft_bridge: Optional[MindcraftBridge] = None
        self.pf: Optional[ProfanityFilter] = None
        self.mm = None
        self.agent_scheduler = None
        # === 弹幕 ===
        self.danmaku_picker = None
        self.bili_svc = None
        self.danmaku_reply_task = None
        # === 插件 ===
        self.plugin_manager: Optional[PluginManager] = None
        # === 内部状态 ===
        self._input_source: str = "text"
        self._pending_stdin_fut: Optional[asyncio.Future] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._reloaders_map: Optional[dict] = None
        self._hydrated_from_slots: bool = False

    # ---- 5.0 新增：从 Kernel slots 映射到 RuntimeContext.xxx 属性 ----

    async def _hydrate_from_slots(self, force: bool = False) -> None:
        """把 Kernel.slots 中激活的实现反映到 self 的对应属性名上。"""
        if self.kernel is None or (self._hydrated_from_slots and not force):
            return
        try:
            from ev.kernel.slots import SlotName
        except Exception as e:
            console.warn(f"[runtime] hydrate 无法导入 SlotName: {e}")
            return
        SLOT_ATTR_MAP: list = [
            (SlotName.MODEL if hasattr(SlotName, "MODEL") else SlotName.model,            "brain"),
            (SlotName.TTS if hasattr(SlotName, "TTS") else SlotName.tts,               "tts"),
            (SlotName.AVATAR if hasattr(SlotName, "AVATAR") else SlotName.avatar,      "vts"),
            (SlotName.INPUT if hasattr(SlotName, "INPUT") else SlotName.input,        "stt_engine"),
            (SlotName.DANMAKU if hasattr(SlotName, "DANMAKU") else SlotName.danmaku,   "bili_svc"),
            (SlotName.MEMORY if hasattr(SlotName, "MEMORY") else SlotName.memory,     "mm"),
            (SlotName.EMOTION if hasattr(SlotName, "EMOTION") else SlotName.emotion,  "emotion_actor"),
            (SlotName.PROACTIVE if hasattr(SlotName, "PROACTIVE") else SlotName.proactive, "proactive"),
            (SlotName.MCP if hasattr(SlotName, "MCP") else SlotName.mcp,               "mcp"),
            (SlotName.BUTLER if hasattr(SlotName, "BUTLER") else SlotName.butler,      "butler"),
            (SlotName.EVOLUTION if hasattr(SlotName, "EVOLUTION") else SlotName.evolution, "evolution"),
            (SlotName.SCHEDULER if hasattr(SlotName, "SCHEDULER") else SlotName.scheduler, "agent_scheduler"),
        ]
        slots = self.kernel.slots
        for slot_enum, attr_name in SLOT_ATTR_MAP:
            impl = slots.get(slot_enum)
            if impl is not None:
                setattr(self, attr_name, impl)
                console.dim(f"[runtime] hydrate: slot.{slot_enum.value} → self.{attr_name} = {type(impl).__name__}")
        self._hydrated_from_slots = True

    async def setup(self) -> None:
        """初始化全部组件（5.0 kernel 路径 + 4.x component 组合式 setup）。"""
        # ---- 5.0 Kernel 路径：一字不动（原 L135-161） ----
        if self.kernel is not None:
            await self._hydrate_from_slots()
            # 挂载 kernel.plugin_manager（如果 kernel 还是 FakePluginManager）
            if self.plugin_manager is None or type(self.plugin_manager).__name__ == "_FakePluginManager":
                try:
                    from plugins.manager import PluginManager as _RealPM
                    real_pm = _RealPM(app=self, kernel=self.kernel, plugins_dir=getattr(self.plugin_manager, "plugins_dir", None) if self.plugin_manager is not None else None)
                    self.plugin_manager = real_pm
                    if hasattr(self.kernel, "attach_plugin_manager"):
                        self.kernel.attach_plugin_manager(real_pm)
                except Exception as e:
                    console.warn(f"[runtime] 5.0 路径 attach PluginManager 失败（可忽略）：{e}")
            else:
                # 已有的 plugin_manager 就 attach 到 kernel
                if hasattr(self.kernel, "attach_plugin_manager") and self.plugin_manager is not None:
                    try: self.kernel.attach_plugin_manager(self.plugin_manager)
                    except Exception: pass
            # boot kernel（会 init/start 插件 → activate slots）；之后再 hydrate 一次
            try:
                await self.kernel.boot()
            except Exception as e:
                console.warn(f"[runtime] kernel.boot 异常：{e}")
            await self._hydrate_from_slots(force=True)
            # 启动完成：直接 return（不执行 4.x 的大段硬编码初始化）
            self._loop = asyncio.get_running_loop()
            return
        # ---- 4.x 旧路径：按 component 顺序串行 setup ----
        from ev.kernel.components import (
            avatar, memory, evolution, llm, tts, mcp, mindcraft,
            proactive, plugin, filter_mod, agent, io_mod,
        )
        self._loop = asyncio.get_running_loop()
        cfg = self.cfg
        # 启动清理（runtime 自己管）
        try:
            from ev.utils import cleaner
            cleaner.cleanup_temp_files(verbose=False)
        except Exception:
            pass
        await avatar.setup(self)
        await memory.setup(self)
        await evolution.setup(self)
        await mcp.setup(self)
        await tts.setup(self)
        await mindcraft.setup(self)
        await filter_mod.setup(self)
        await llm.setup(self)
        await proactive.setup(self)
        await plugin.setup(self)
        await agent.setup(self)
        await io_mod.setup(self)
        # VTS model change listener（依赖 self.vts/self.face 已初始化）
        await avatar.attach_model_change_listener(self)

    async def teardown(self) -> None:
        """优雅关闭全部组件（按原 teardown 顺序逆序调用 component.teardown）。"""
        from ev.kernel.components import (
            avatar, memory, evolution, llm, tts, mcp, mindcraft,
            proactive, plugin, filter_mod, agent, io_mod,
        )
        # 顺序与原 teardown 完全一致：插件→弹幕→STT→mindcraft→TTS→MCP→字幕/face/VTS
        await plugin.teardown(self)
        await io_mod.teardown(self)       # bili + stt
        await mindcraft.teardown(self)
        await tts.teardown(self)
        await mcp.teardown(self)
        # sub / face / vts 在 avatar.teardown 里一起处理
        await avatar.teardown(self)
        await proactive.teardown(self)
        await llm.teardown(self)
        await filter_mod.teardown(self)
        await agent.teardown(self)
        await evolution.teardown(self)
        await memory.teardown(self)
