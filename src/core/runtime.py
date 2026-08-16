"""运行时上下文：组件容器 + 共享能力（§3.7 拆分）。

承接原 Application 的组件初始化 / 清理 / 跨域共享方法，使 Application
瘦身为纯编排层。拆分原则：逻辑零改动、仅把 self 引用改为容器内访问，
外部接口（main.py → run_with_cleanup → Application.run）保持不变。
"""

import asyncio
import json
import os
from datetime import datetime
from typing import Optional

from src.utils import config, console
from src.utils.constants import (
    ROLE_AI_ALIAS, SOURCE_DANMAKU_INPUT, SOURCE_DANMAKU_REPLY,
)
from tools.memory import memory
from src.llm import stream
from src.llm.agent import ButlerAgent
from src.llm.evolution import EvolutionEngine
from src.mcp.manager import MCPManager
from src.llm.proactive import ProactiveEngine
from src.vts.face_driver import FaceDriver
from src.llm.llm_brain import LLMBrain
from src.vts.model_scanner import scan_model
from src.tts.engine import TTSEngine
from src.vts.controller import VTSController
from src.mindcraft.bridge import MindcraftBridge
from src.utils.perf_tracker import PerfTracker
from src.utils.subtitle_server import SubtitleServer
from src.core.commands import CommandRegistry
from src.core.output_lock import (
    STATE_AI_SPEAKING, STATE_IDLE, get_output_lock, get_output_owner,
    set_danmaku_pending, set_output_owner, set_global_state,
)
from plugins import PluginManager, UserInputEvent
from src.core.bus import EV_USER_INPUT, bus
from src.core.error_handler import report_error
from src.core.events.models import InputEvent
from src.utils.content_filter import ProfanityFilter
from src.utils.safe_text import sanitize_external

# 记忆自动整合蒸馏：碎片条数 ≥ 阈值时触发 AI 蒸馏合并（成功后才删除旧碎片），
# 后台循环检查间隔（秒）。阈值 60 = 约 4 批 × 15 条，正常会话去重后很少触达。
_MEMORY_INTEGRATE_THRESHOLD = 60
_MEMORY_INTEGRATE_INTERVAL = 2 * 3600


class RuntimeContext:
    """E.V 运行时所有组件的容器与共享能力（取代 Application 的组件持有职责）。"""

    def __init__(self, cfg) -> None:
        self.cfg = cfg
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
        # 命令注册表：由编排层（Application）在 setup 后构建并注入
        self._cmd_registry: Optional[CommandRegistry] = None
        # 细粒度配置热重载器注册表（懒构建，供 !config <组件> 与全量共用）
        self._reloaders_map: Optional[dict] = None

    # ---------- 生命周期：setup / teardown ----------

    async def setup(self) -> None:
        """初始化全部组件（原 Application.run 的初始化段，逻辑零改动）。"""
        cfg = self.cfg
        # 主事件循环引用：播放线程回调（说话结束复原）需经它调度回主循环
        self._loop = asyncio.get_running_loop()

        # 启动清理上次残留临时文件
        try:
            from src.utils import cleaner
            cleaner.cleanup_temp_files(verbose=False)
        except Exception:
            pass

        vts_ok = False
        if cfg.RUN_MODE == "pet":
            from src.pet.widget import PetWidget, BubbleSub
            from src.pet.driver import PetFaceDriver

            console.kv("模型", f"{cfg.LLM_MODEL}（深度思考 {'开' if cfg.LLM_THINKING else '关'}）")
            console.kv("桌宠", os.path.basename(cfg.PET_MODEL_PATH))
            console.dim("拖动模型移动位置 | 点击模型播放动作 | 输入 /quit 退出")
            if cfg.EMOTION_ACTOR_ENABLED:
                console.dim("表情/动作：Embedding 情绪自动控制已启用（用户消息分类情绪播放；"
                            "也可用 /expr /motion /face list 手动控制）")
            else:
                console.dim("表情/动作：自动情绪控制未启用（/expr /motion /face list 手动控制仍可用）")

            self.pet_widget = PetWidget(cfg)
            self.pet_widget.show()
            # 表情/动作 actor 总是创建：手动命令与控制中心试播不依赖自动控制开关，
            # 自动分类播放由调用点按 EMOTION_ACTOR_ENABLED 开关控制
            from src.pet.emotion_actor import PetEmotionActor
            self.emotion_actor = PetEmotionActor(self.pet_widget, cfg)
            self.emotion_actor.scan()
            self.emotion_actor.load_map()
            self.pet_widget.on_model_loaded = lambda w: self.emotion_actor.scan()
            self.face = PetFaceDriver(self.pet_widget, cfg)
            self.pet_widget.attach_driver(self.face)
            self.face.start()
            if cfg.PET_MOTION_PATH:
                self.face.set_motion(cfg.PET_MOTION_PATH)
            self.sub = BubbleSub(self.pet_widget)

            # 桌宠模型热切换兜底监控
            async def _watch_pet_model_change() -> None:
                env_file = os.path.join(cfg.PROJECT_ROOT, ".env")
                active = str(cfg.PET_MODEL_PATH or "").strip()
                while True:
                    await asyncio.sleep(2.0)
                    try:
                        if not os.path.isfile(env_file):
                            continue
                        new_path = ""
                        with open(env_file, "r", encoding="utf-8") as f:
                            for line in f:
                                line = line.strip()
                                if line.startswith("PET_MODEL_PATH="):
                                    new_path = line.split("=", 1)[1].strip()
                                    new_path = new_path.strip('"').strip("'")
                                    break
                        if new_path and new_path != active:
                            active = new_path
                            if self.pet_widget.switch_model(new_path):
                                console.ok(
                                    f"检测到模型配置变更，已热切换桌宠：{new_path}")
                            else:
                                console.warn(f"模型配置变更但切换失败：{new_path}")
                    except Exception:
                        pass

            asyncio.create_task(_watch_pet_model_change())
        else:
            console.kv("模型", f"{cfg.LLM_MODEL}（深度思考 {'开' if cfg.LLM_THINKING else '关'}）")
            console.kv("VTS", f"端口 {cfg.VTS_PORT}")
            console.dim("AI 全权接管：自动眨眼 / 呼吸 / 身体摇摆 | 基线动画由 .motion3.json 驱动")
            console.dim("输入 /quit 退出")

            self.vts = VTSController()
            vts_ok = await self.vts.connect()
            if not vts_ok:
                console.warn("将以「纯对话」模式运行，无口型/动作/表情控制。")

            self.sub = SubtitleServer().start()
            console.dim(f"字幕网页：http://127.0.0.1:{self.sub.port}/（打字机效果，浏览器打开即可）")

        # ButlerAgent 记忆管家
        if cfg.MEMORY_ENABLED:
            self.butler = ButlerAgent()
        else:
            self.butler = None

        # 自我进化引擎（对话后后台复盘，随配置开关创建）
        self.evolution = EvolutionEngine() if cfg.EVOLUTION_ENABLED else None

        # MCP 管理器
        self.mcp = MCPManager() if (cfg.MCP_ENABLED and cfg.TOOLS_ENABLED) else None
        if self.mcp is not None:
            await self.mcp.initialize()
        from plugins.tools import get_merged_tools
        merged_tools = get_merged_tools(self.mcp)
        if merged_tools:
            names = [t["function"]["name"] for t in merged_tools]
            console.dim(f"工具已就绪（{len(names)} 个）：{'、'.join(names)}")
        else:
            console.dim("无可用工具：AI 将以纯对话模式运行（MCP 未启用且未配置搜索/天气 key）")

        # 技能系统
        from plugins.tools.skills import get_skill_manager
        skill_mgr = get_skill_manager()
        if skill_mgr.skills:
            names = [s.name for s in skill_mgr.skills]
            console.dim(f"技能已就绪（{len(names)} 个）：{'、'.join(names)}"
                        f"（load_skill 按需加载，改文件无需重启）")
        else:
            console.dim("技能目录为空：未发现技能（SKILLS_DIR/<技能名>/SKILL.md）")

        # vtuber 模式：启动时模型扫描适配
        profile = None
        if vts_ok and self.vts is not None:
            profile = await scan_model(self.vts, cfg)
            if cfg.MOUTH_PARAMETER and profile.mouth_param:
                profile.mouth_param = cfg.MOUTH_PARAMETER
            if profile.mouth_param:
                cfg.MOUTH_PARAMETER = profile.mouth_param
                cfg.MOUTH_GAIN = profile.mouth_gain

            self.face = FaceDriver(self.vts, profile)
            self.face.start()
            if cfg.MOTION_PATH:
                self.face.set_motion(cfg.MOTION_PATH)
            elif cfg.VTS_IDLE_TAKEOVER and profile.idle_motion:
                self.face.set_motion(profile.idle_motion)
                console.ok(f"待机动画接管：{os.path.basename(profile.idle_motion)}"
                          f"（P2 覆盖 VTS 待机，循环点已平滑）")

            # 表情/动作演员（VTS 模式）：用户消息 → Embedding 情绪分类 → 播放表情/动作。
            # 总是创建：手动命令与控制中心试播不依赖自动控制开关，
            # 自动分类播放由主循环按 EMOTION_ACTOR_ENABLED 开关控制
            from src.vts.emotion_actor import VtsEmotionActor
            self.emotion_actor = VtsEmotionActor(self.vts, cfg, face=self.face)
            await self.emotion_actor.scan()
            self.emotion_actor.load_map()
            if cfg.EMOTION_ACTOR_ENABLED:
                console.dim("表情/动作：Embedding 情绪自动控制已启用（用户消息分类情绪播放；"
                            "也可用 /expr /motion /face list 手动控制）")
            else:
                console.dim("表情/动作：自动情绪控制未启用（/expr /motion /face list 手动控制仍可用）")

        # Embedding 预热：情绪分类器是惰性初始化（第一条用户消息才构建 provider
        # 并对语料批量向量化），启动后台提前构建，避免首轮消息的冷启动延迟。
        # 与 TTS / LLM 连接预热同一模式：失败静默，不影响启动。
        actor = getattr(self, "emotion_actor", None)
        if actor is not None and cfg.EMOTION_ACTOR_ENABLED:
            async def _warm_emotion_classifier() -> None:
                try:
                    await actor.initialize()
                except Exception:
                    pass
            asyncio.create_task(_warm_emotion_classifier())

        # TTS 引擎：后台并行加载（本地模型 + 参考音频预编码约 20s），
        # 不阻塞 LLM / 记忆等后续初始化，启动即开始加载；
        # 未就绪时 speak/drain/stop 由引擎内部守卫静默处理，就绪后自动可用
        self.tts = None
        if cfg.GPTSOVITS_REF_AUDIO:
            self.tts = TTSEngine()
            asyncio.create_task(self.init_tts_async())

        # 磁盘音频缓存清理：与 TTS 服务连接状态解耦，启动即后台执行
        # （引擎连接成功路径也有一次清理，幂等，覆盖「服务晚于主程序启动」场景）
        try:
            from src.tts.engine import evict_tts_cache
            asyncio.create_task(asyncio.to_thread(evict_tts_cache))
        except Exception:
            pass

        # Mindcraft 双向桥（socket.io 连接 MindServer）：开关开启才创建，
        # 由后台循环负责连接/重连（引擎可能晚于主程序启动）。
        self.mindcraft_bridge = None
        if cfg.MINDCRAFT_BRIDGE_ENABLED:
            self.mindcraft_bridge = MindcraftBridge(
                server_url=f"http://127.0.0.1:{cfg.MINDCRAFT_MINDSERVER_PORT}",
                agent_name=cfg.MINDCRAFT_BOT_NAME,
                on_bot_output=self._on_mindcraft_bot_output,
            )
            asyncio.create_task(self._mindcraft_loop())

        # 内容过滤（弹幕回复 / 主动对话 / 用户对话共用，需在引擎创建前就绪）
        self.pf = ProfanityFilter() if cfg.PROFANITY_FILTER_ENABLED else None
        if self.pf is not None:
            console.dim(f"内容过滤已启用：检测到骂人用语时 "
                        f"{cfg.PROFANITY_FILTER_RATE:.0%} 概率触发（替换为 Filter）")

        # 初始化记忆系统
        self.mm = memory.get_manager()
        self.mm.load()
        self.mm.new_session()
        if cfg.MEMORY_ENABLED:
            # 上次运行时 remember/forget 失败队列（drain 期间要等 memory service
            # 就绪，所以放后台任务里）
            async def _drain_retry_after_load() -> None:
                from plugins.tools import memory_tools
                # 等几帧让 mm.load() 完成 service 初始化
                await asyncio.sleep(0.5)
                try:
                    n = await asyncio.to_thread(memory_tools.drain_retry_queue)
                    if n:
                        console.dim(f"[记忆] 启动重放成功 {n} 条暂存记忆")
                except Exception as e:
                    console.dim(f"[记忆] 重放失败队列失败：{e}")
            asyncio.create_task(_drain_retry_after_load())
            asyncio.create_task(memory.warmup())
            # 记忆时间衰减：后台定时清理长期未更新的非固定记忆
            asyncio.create_task(memory.decay_loop())
            # AI 自动整合蒸馏：碎片过多时后台蒸馏合并并删除旧条目
            asyncio.create_task(self._memory_integration_loop())

        self.brain = LLMBrain(mcp=self.mcp)

        # 连接预热：启动空闲期发一个最小请求，把 TLS 握手冷启动挪到后台，
        # 用户第一次提问即命中热连接（首轮 TTFT 实测 2565ms → 热连接 ~1s）
        asyncio.create_task(self.brain.warmup())

        if cfg.MEMORY_ENABLED:
            console.dim(f"记忆系统：已启用（{memory.count()} 个记忆文件）")

        # 主动对话引擎
        self.proactive = None
        if cfg.PROACTIVE_ENABLED:
            self.proactive = ProactiveEngine(
                brain=self.brain, tts=self.tts, face=self.face, sub=self.sub, cfg=cfg,
                butler=self.butler if cfg.MEMORY_ENABLED else None,
                memory_manager=self.mm if cfg.MEMORY_ENABLED else None,
                profanity_filter=self.pf,
                profanity_filter_rate=cfg.PROFANITY_FILTER_RATE,
            )
            console.dim(
                f"主动对话已启用：LLM 自主开口（互动/弹幕结束即给机会，"
                f"静默期每 {cfg.RESPONSE_INTERVAL_MIN:.0f}~"
                f"{cfg.RESPONSE_INTERVAL_MAX:.0f}s 随机给一次机会，"
                f"是否开口由主模型自主决定）")
        else:
            console.dim("主动对话未启用（.env 设置 PROACTIVE_ENABLED=true 开启）")

        # 插件系统：加载 plugins/ 下启用的插件（Python 同进程 async 运行时，
        # 对标 live-2d 插件体系——钩子 / 工具 / 定时器，目录约定见 plugins/README.md）
        try:
            from plugins.manager import set_default_manager
            self.plugin_manager = PluginManager(self)
            await self.plugin_manager.load_all()
            await self.plugin_manager.start_all()
            # 供工具合并（get_merged_tools）与 speak_text 读取
            self.brain.plugin_manager = self.plugin_manager
            set_default_manager(self.plugin_manager)
        except Exception as e:
            self.plugin_manager = None
            console.warn(f"[插件] 插件系统初始化失败（不影响运行）：{e}")

        # 自我进化：定期自我提示（空闲期主动补复盘，后台循环）
        if self.evolution is not None:
            asyncio.create_task(self._evolution_periodic_loop())

        # 语音识别
        if cfg.STT_ENABLED:
            try:
                from src.asr.stt import STTEngine
                self.stt_engine = STTEngine(cfg)
                self.stt_engine.start()
                console.dim(
                    f"语音识别已启用：对着麦克风说话即可输入"
                    f"（{cfg.STT_MODEL}，静音 {cfg.STT_SILENCE_SECONDS:.0f}s 自动切段）")
            except Exception as e:
                self.stt_engine = None
                console.warn(f"语音识别启动失败（可忽略）：{e}")
        else:
            console.dim("语音识别未启用（.env 设置 STT_ENABLED=true 开启）")

        # B 站弹幕服务启动
        self.danmaku_picker, self.bili_svc, self.danmaku_reply_task = await self.start_bili()

        # VTS 模型切换事件订阅
        if vts_ok:
            _scanning = False

            async def _rescan_on_model_switch(msg: dict) -> None:
                nonlocal _scanning
                if not msg.get("data", {}).get("modelLoaded") or _scanning:
                    return
                console.info("检测到模型切换，暂停伪面捕并重新扫描适配...")
                _scanning = True
                try:
                    await self.face.stop()
                    new_profile = await scan_model(self.vts, self.cfg)
                    if not new_profile.model_name:
                        console.warn("新模型未加载，跳过适配（保留当前状态）")
                        return
                    if self.cfg.MOUTH_PARAMETER and new_profile.mouth_param:
                        new_profile.mouth_param = self.cfg.MOUTH_PARAMETER
                    if new_profile.mouth_param:
                        self.cfg.MOUTH_PARAMETER = new_profile.mouth_param
                        self.cfg.MOUTH_GAIN = new_profile.mouth_gain
                    self.face.apply_profile(new_profile)
                    console.ok(f"已适配新模型「{new_profile.model_name}」")
                    if self.cfg.MOTION_PATH:
                        self.face.set_motion(self.cfg.MOTION_PATH)
                    elif self.cfg.VTS_IDLE_TAKEOVER:
                        if new_profile.idle_motion:
                            self.face.set_motion(new_profile.idle_motion)
                            console.ok(f"待机动画接管：{os.path.basename(new_profile.idle_motion)}")
                        else:
                            # 新模型无待机动画可接管：停止旧模型遗留的动作注入
                            self.face.stop_motion()
                    # 表情/动作演员重扫（新模型的表情/动画热键不同）
                    if self.emotion_actor is not None:
                        try:
                            await self.emotion_actor.scan()
                        except Exception as e:
                            console.dim(f"表情/动作重扫失败：{e}")
                finally:
                    _scanning = False
                    self.face.start()

            self.vts.on_event("ModelLoadedEvent", _rescan_on_model_switch)
            if await self.vts.subscribe_event("ModelLoadedEvent"):
                console.dim("已订阅模型切换事件：运行中切换模型将自动重新适配")

    async def teardown(self) -> None:
        """优雅关闭全部组件（原 Application.run 的 finally 清理段，逻辑零改动）。"""
        # 清理：停插件 → 停弹幕 → 停字幕 → 排空 TTS → 关 TTS → 停面捕 → 关 VTS
        if self.plugin_manager is not None:
            try:
                await self.plugin_manager.stop_all()
            except Exception:
                pass
        cancel = self.stop_bili()
        self.danmaku_picker, self.bili_svc, self.danmaku_reply_task = None, None, None
        if cancel is not None:
            try:
                await cancel
            except (asyncio.CancelledError, Exception):
                pass
        self.sub.stop()
        if self.mindcraft_bridge is not None:
            try:
                await asyncio.wait_for(
                    self.mindcraft_bridge.disconnect(), timeout=5)
            except Exception:
                pass
        if self.stt_engine is not None:
            self.stt_engine.stop()
        if self.mcp is not None:
            try:
                await asyncio.wait_for(self.mcp.stop(), timeout=15)
            except Exception:
                pass
        if self.tts is not None:
            try:
                # 停止时先打断播放：drain 立即返回。否则若队列里还有
                # 音频，drain 会等它播完（最多 15s），停止明显变慢。
                self.tts.interrupt()
                await asyncio.wait_for(self.tts.drain(), timeout=5)
                await asyncio.wait_for(self.tts.stop(), timeout=15)
            except Exception:
                pass
        if self.face is not None:
            try:
                await asyncio.wait_for(self.face.stop(), timeout=10)
            except Exception:
                pass
        if self.vts is not None:
            try:
                await asyncio.wait_for(self.vts.close(), timeout=10)
            except Exception:
                pass

    # ---------- 弹幕服务启停 ----------

    def stop_bili(self):
        """关闭弹幕服务；返回 reply_task cancel 协程或 None。"""
        cancel_coro = None
        if self.danmaku_picker is not None:
            try:
                self.danmaku_picker.stop()
                from src.danmaku.bili_danmaku import set_danmaku_picker
                set_danmaku_picker(None)
            except Exception as e:
                console.dim(f"[弹幕] 挑选器停止失败（不影响整体退出）：{e}")
        if self.bili_svc is not None:
            try:
                self.bili_svc.stop()
            except Exception as e:
                console.dim(f"[弹幕] 服务停止失败（不影响整体退出）：{e}")
        if self.danmaku_reply_task is not None and not self.danmaku_reply_task.done():
            self.danmaku_reply_task.cancel()
            cancel_coro = self.danmaku_reply_task
        return cancel_coro

    async def start_bili(self):
        """启动 B 站弹幕服务（多房间）+ DanmakuPicker + 回复协程。"""
        cfg = self.cfg
        room_ids = cfg.BILI_ROOM_IDS or (
            [cfg.BILI_ROOM_ID] if cfg.BILI_ROOM_ID else [])
        if not (cfg.BILI_ENABLED and room_ids):
            if cfg.BILI_ENABLED and not room_ids:
                console.dim("[弹幕] BILI_ENABLED=true 但未配置房间号"
                            "（BILI_ROOM_ID/BILI_ROOM_IDS），不启用弹幕精选回复")
            return None, None, None
        try:
            from src.danmaku.bili_danmaku import (
                BiliServiceManager, DanmakuPicker, set_danmaku_picker,
            )
            from src.danmaku.client import bili_loop
        except Exception as e:
            console.warn(f"[弹幕] 模块导入失败：{e}")
            return None, None, None

        queue: asyncio.Queue = asyncio.Queue(maxsize=64)
        loop = asyncio.get_running_loop()
        html_path = os.path.join(cfg.PROJECT_ROOT, "ui", "弹幕卡片.html")
        mgr = BiliServiceManager(room_ids, cfg.BILI_SERVER_PORT, html_path)
        # 注入 client.py 的线程启动函数：缺失会导致 BiliService.start()
        # 抛 "BiliService.set_client_starter 未调用"（多房间改造遗漏的注入点）
        mgr.attach_client_starter(bili_loop)
        mgr.start()

        def _enqueue(items: list) -> None:
            """把一条/批量精选弹幕送入回复队列。"""
            try:
                # 弹幕回复已敲定：置标记，主动对话在此期间避让不抢话
                set_danmaku_pending(True)
                loop.call_soon_threadsafe(queue.put_nowait, items)
            except Exception as e:
                console.error(f"[弹幕] 入队失败：{e}")

        def _on_pick(uid: int, username: str, text: str) -> None:
            _enqueue([(uid, username, text)])

        def _on_batch_pick(items: list) -> None:
            _enqueue(items)

        picker = DanmakuPicker(_on_pick, on_batch_reply_callback=_on_batch_pick)
        set_danmaku_picker(picker)
        task = asyncio.create_task(
            self._danmaku_reply_loop(queue),
            name="danmaku_reply_loop",
        )
        return picker, mgr, task

    # ---------- 弹幕回复链路 ----------

    async def _danmaku_reply_loop(self, q) -> None:
        """后台协程：从弹幕队列取精选（单条或批量），走 _chat_danmaku 完整对话链路。"""
        while True:
            try:
                items = await q.get()
                await self._chat_danmaku(items)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                console.error(f"[弹幕] 回复出错：{e}")
                await report_error(e, msg=f"[弹幕] 回复出错：{e}")

    async def _chat_danmaku(self, items) -> None:
        """弹幕精选回复：走完整的 LLM→TTS→字幕→口型→记忆链路。

        items: [(uid, username, text), ...]；单条弹幕 = 1 个元素（与原来完全一致），
        多条 = 高密度批量聚合回复（"观众 A 说 X，观众 B 说 Y，你怎么看"），
        走一次完整对话，避免逐条回复刷屏。

        注意：到达这里的弹幕已经过 ProfanityFilter（data/profanity.txt）过滤，
        命中词库的弹幕在 _on_danmaku 入口就被丢弃（不显示、不回复）。
        所以这里直接用原文，不做任何替换/过滤。

        若当前已有播报在进行（主动对话 / 用户对话 / 弹幕），本条弹幕
        直接丢弃、不排队等锁——播放结束后才重新接受新的弹幕，避免
        「主动播完立刻又接弹幕」的连续开口。
        """
        cfg = self.cfg
        # 外部不可信文本（弹幕/昵称）先净化：防 prompt 注入标签与控制字符
        # 进入 LLM prompt / 记忆库 / 前端展示（见 src/utils/safe_text.py）
        items = [(uid, sanitize_external(nick) or "匿名", sanitize_external(t))
                 for uid, nick, t in items]
        # 主弹幕：picker 已按分数降序排列，items[0] 为最高分那条，用于展示/记忆归位
        uid, username, text = items[0]
        extra = len(items) - 1

        # 正在播报中（锁被占用）：丢弃本条，播放完才接受新弹幕。
        # 注意：_on_pick 已置位「弹幕回复待播报」标记，此处须按情况清除——
        # 锁被另一条弹幕占用：其播完的 finally 会清 pending，这里不能抢清
        # （否则正在播报的弹幕还没说完，主动对话就恢复触发）；
        # 锁被主动/用户占用：本条被跳过后无人再清 pending，必须立即清除，
        # 否则主动对话被永久静音。
        if get_output_lock().locked():
            console.dim(f"[弹幕] 正在播报，跳过本条（{username}：{text}）")
            if get_output_owner() != "danmaku":
                set_danmaku_pending(False)
            return

        # 通过跳过检查、真正开始回复：左栏「对话」显示本条弹幕（被跳过的
        # 弹幕不显示），同时推 SSE 让前端卡片展示
        # （被跳过/未真正回复的弹幕不上卡片，只留在左侧实时流）
        if extra:
            shown = f"{text}（等{extra}条弹幕）"
        else:
            shown = text
        console.chat(f"精彩弹幕：{shown}")
        # 事件总线：观众弹幕进入内核
        await bus.emit(EV_USER_INPUT, InputEvent(
            source="barrage", content=shown, sender=username,
            metadata={"uid": uid, "extra": extra}))
        if self.bili_svc is not None:
            try:
                self.bili_svc.broadcast({
                    "type": "reply",
                    "username": username,
                    "text": shown,
                    "uid": uid,
                })
            except Exception:
                pass

        if extra:
            # 批量回复：多条弹幕并进一个 prompt，AI 合并回应（不逐条念）
            danmaku_lines = "\n".join(
                f"- 观众{nick}：{t}" for _, nick, t in items)
            wrapped = (
                f"[系统提示] 现在你在直播间，刚收到几条观众弹幕，请合并回应"
                f"（不要逐条念，抓住共同话题自然地回一句）。\n{danmaku_lines}\n"
                f"请用 stream-chat 技能的直播闲聊风格回复（先回应情绪或接话，"
                f"一句话说完即可，不要连续追问）。"
            )
        else:
            wrapped = (
                f"[系统提示] 现在你在直播间，收到观众弹幕请自然地回一句。"
                f"观众昵称：{username}，弹幕内容：\n{text}\n"
                f"请用 stream-chat 技能的直播闲聊风格回复（先回应情绪或接话，"
                f"一句话说完即可，不要连续追问）。"
            )

        # 插件钩子：onUserInput（source="barrage"，可改写 prompt / 注入背景 / 拦截回复）
        if self.plugin_manager is not None:
            event = UserInputEvent(wrapped, "barrage")
            await self.plugin_manager.run_user_input_hooks(event)
            if event.prevented:
                console.dim("[插件] 弹幕回复被插件拦截")
                # 本条跳过且无人再清「弹幕回复待播报」标记，必须立即清除
                set_danmaku_pending(False)
                return
            wrapped = event.text
            if event.contexts:
                self.brain.push_turn_context(event.contexts)

        # 主动引擎：有观众说话也算"有人互动"——清 0 一下孤独/无聊
        if self.proactive is not None:
            try:
                self.proactive.on_user_message()
            except Exception:
                pass

        # —— 每轮对话性能埋点 ——
        turn_tracker = PerfTracker(f"弹幕回复@{username}")
        turn_tracker.begin("端到端")

        # 弹幕内容也推送到字幕网页（打字机气泡）：多条时合并一行
        try:
            self.sub.push("user", " ｜ ".join(
                f"@{nick}：{t}" for _, nick, t in items))
        except Exception:
            pass

        _turn_pairs = [(nick, t) for _, nick, t in items]

        async def _on_llm_done(reply_text: str) -> None:
            if not (cfg.MEMORY_ENABLED and self.butler and self.mm):
                return
            try:
                # 先取快照：避免把本次新增的弹幕轮次混进上下文
                prev_turns = self.mm.recent_turns[:]
                for nick, t in _turn_pairs:
                    self.mm.add_turn("user", f"[弹幕@{nick}] {t}",
                                     source=SOURCE_DANMAKU_INPUT)
                self.mm.add_turn(ROLE_AI_ALIAS, reply_text,
                                 source=SOURCE_DANMAKU_REPLY)
                user_msgs = [{"role": "user",
                              "content": f"[弹幕@{nick}] {t}"}
                             for nick, t in _turn_pairs]
                await self.butler.submit_extract_and_store(
                    user_msgs + [{"role": "assistant",
                                  "content": reply_text}],
                    prev_turns,
                )
            except Exception as e:
                console.dim(f"[ButlerAgent] 弹幕记忆提取出错：{e}")
            # 自我进化：复盘走管家模型（agent 配置），后台执行不阻塞弹幕播报
            if self.evolution is not None:
                try:
                    asyncio.create_task(self.evolution.maybe_review(
                        self.mm.recent_turns, proactive=self.proactive))
                except Exception:
                    pass

        try:
            # 全局输出互斥（owner="danmaku"）+ 不带打断监听的 stream.converse：
            # - 锁防止用户/主动并发抢占；
            # - owner 标记让 _wait_input 丢弃此间到达的输入；
            # - 无打断监听保证内部不会自己跳。
            output_lock = get_output_lock()
            acquired = False
            async with output_lock:
                acquired = True
                set_output_owner("danmaku")
                set_global_state(STATE_AI_SPEAKING)
                # 复位残留打断标志：用户对话中途被打断后 _interrupted 仍为 True，
                # 若不复位，本条弹幕回复的句子会被 speak()/_pump 全部丢弃，
                # 表现为「弹幕回复播放不完整（甚至完全无声）」。
                if self.tts is not None:
                    try:
                        self.tts.clear_interrupt()
                    except Exception:
                        pass
                await stream.converse(
                    self.brain, wrapped, tts=self.tts, face=self.face, sub=self.sub,
                    profanity_filter=self.pf,
                    profanity_filter_rate=cfg.PROFANITY_FILTER_RATE,
                    on_llm_done=_on_llm_done if cfg.MEMORY_ENABLED else None,
                )
                # 左栏对话换行：回复句子是连续流（无换行），收尾补一个，
                # 避免与下一条弹幕/发言粘连成一行
                console.chat()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            console.error(f"弹幕回复出错：{e}")
        finally:
            # 兜底恢复：真正拿到锁才还原 owner/状态（避免覆盖并发说话者）；
            # 「弹幕回复待播报」标记无论是否开场都要清除（含等锁期间取消），
            # 否则主动对话会被永久静音。
            if acquired:
                set_output_owner(None)
                set_global_state(STATE_IDLE)
            set_danmaku_pending(False)

        turn_tracker.end("端到端")
        turn_tracker.print_report()

    # ---------- 记忆整合 / 进化后台 ----------

    async def _memory_integration_loop(self, interval: float = _MEMORY_INTEGRATE_INTERVAL) -> None:
        """后台循环：记忆碎片超过阈值时由 AI 蒸馏合并，并删除已蒸馏的旧条目。"""
        while True:
            try:
                await self._integrate_memories()
            except Exception as e:
                console.dim(f"[记忆整合] 检查出错（不影响运行）：{e}")
            await asyncio.sleep(interval)

    async def _integrate_memories(self) -> None:
        """AI 自动蒸馏整库记忆：蒸馏成功才删除旧碎片，失败保留原记忆。"""
        if not (self.cfg.MEMORY_ENABLED and self.butler is not None
                and self.mm is not None):
            return
        try:
            files = self.mm.list_files()
        except Exception as e:
            console.dim(f"[记忆整合] 读取记忆库失败：{e}")
            return
        if len(files) < _MEMORY_INTEGRATE_THRESHOLD:
            return
        console.dim(f"[记忆整合] 记忆碎片达 {len(files)} 条，开始 AI 蒸馏整合...")
        entries = await self.butler.integrate_memories(files)
        if not entries:
            console.dim("[记忆整合] 无可用蒸馏结果，保留原记忆")
            return
        # 删除前备份：蒸馏合并为破坏性操作，留一份快照便于回滚
        try:
            backup_path = os.path.join(
                self.cfg.DATA_ROOT,
                f"backup_memories_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
            with open(backup_path, "w", encoding="utf-8") as f:
                json.dump(files, f, ensure_ascii=False, indent=1)
            console.dim(f"[记忆整合] 已备份 -> {backup_path}")
        except OSError as e:
            console.dim(f"[记忆整合] 备份失败（继续整合）：{e}")
        ids = [str(f.get("id")) for f in files if f.get("id")]
        deleted = self.mm.delete_memories(ids)
        result = await self.mm.commit_recall_files(entries)
        written = len(result.get("recall_files") or [])
        console.dim(f"[记忆整合] 完成：删除 {deleted} 条碎片，"
                    f"写入 {written} 条新记忆（当前共 {self.mm.count()} 条）")

    async def _evolution_periodic_loop(self) -> None:
        """后台循环：每 EVOLUTION_PERIODIC_INTERVAL 秒检查一次，空闲期主动补复盘。

        仅当距上次复盘已达标且存在未复盘的新对话轮次时才调用 LLM
        （见 EvolutionEngine.periodic_tick 的节流判定），不重复消费 token。
        """
        interval = max(60, int(self.cfg.EVOLUTION_PERIODIC_INTERVAL or 1800))
        while True:
            await asyncio.sleep(interval)
            if self.evolution is None:
                continue
            try:
                await self.evolution.periodic_tick(
                    self.mm.recent_turns if self.mm is not None else [],
                    proactive=self.proactive)
            except Exception as e:
                console.dim(f"[自我进化] 周期任务出错（不影响运行）：{e}")

    # ---------- 会话归档 ----------

    async def archive_session(self) -> None:
        """退出前会话归档：摘要写入档案 + 蒸馏记忆写回记忆库。

        含两轮 LLM 调用（会话摘要 + 蒸馏）。两轮互不依赖，并行执行——
        串行时停止最坏要等两轮 LLM；并行后耗时 ≈ 单轮，明显缩短停止等待。
        退出时由 _shutdown 包一层 20s 总额超时兜底，避免 LLM 完全无响应
        时退出无限等待。
        """
        turns = self.mm.recent_turns
        summary, entries = await asyncio.gather(
            self.butler.summarize_session(turns[-30:]),
            self.butler.distill_session(turns[-30:]),
        )
        if summary:
            await self.mm.update_archive_async(
                summary=summary,
                period_start=self.mm.started_at or turns[0].get("timestamp", ""),
                period_end=datetime.now().isoformat(),
            )
        if entries:
            await self.mm.commit_recall_files(entries)

    # ---------- TTS / 情绪 ----------

    async def init_tts_async(self) -> None:
        """后台初始化 TTS 引擎（本地模型 + 参考音频预编码约 20s），不阻塞启动流程。

        run() 中创建 TTS 引擎后立即后台启动加载（asyncio.create_task）；
        就绪前 speak/drain/stop 由引擎内部守卫静默处理，就绪后自动可用。
        """
        try:
            tts_ok = await self.tts.start()
        except Exception as e:
            console.warn(f"TTS 后台初始化异常：{e}")
            tts_ok = False
        if not tts_ok:
            self.tts = None
            return
        # 预热：合成一句短文本让服务端 CUDA graph / 缓存提前就绪，
        # 降低主播第一次真实说话时的首字延迟（失败不影响使用）
        try:
            await self.tts.warmup()
        except Exception:
            pass  # warmup 内部已捕获，这里仅兜底
        # 口型回调只在有脸部驱动器时注册：纯对话模式（无 VTS/桌宠）下
        # self.face 为 None，注册了会每块音频都报
        # 'NoneType' object has no attribute 'load_speech_curve'
        if self.face is not None:
            def _on_tts_play(wav: str, text: str, dur_s: float) -> None:
                if not self.face.load_speech_curve(wav):
                    self.face.start_speaking(dur_s)

            self.tts.set_on_play_callback(_on_tts_play)
        # 说话结束复原：表情/动作只在说话期间显示，播完（含打断）恢复默认
        if self.emotion_actor is not None:
            self.tts.set_on_play_done_callback(self._restore_emotion_after_speech)
        if self.sub:
            self.tts.set_subtitle_callback(lambda t: self.sub.push("text", t))

    def _restore_emotion_after_speech(self) -> None:
        """整段说话结束（播完或打断）→ 表情/动作复原。

        回调在播放线程触发，经主事件循环调度 actor.restore()（async 方法），
        保证与 TTS/情绪分类等主循环任务互斥。
        """
        actor, loop = self.emotion_actor, self._loop
        if actor is None or loop is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(actor.restore(), loop)
        except Exception as e:
            console.dim(f"表情/动作复原调度失败：{e}")

    async def speak_memory_reply(self, text: str) -> None:
        """记忆指令确认播报：控制台 + 字幕 + TTS（走全局输出锁，互斥不打断）。"""
        if self.tts is None:
            # 无 TTS：直接推整句字幕（没有播放时机，逐字推进无从谈起）
            if self.sub is not None:
                self.sub.push("text", text)
            console.ok(text)
            return
        # 有 TTS：字幕交给 TTS 引擎的字幕管线逐字推进，此处不预推整句，
        # 否则整句会先于语音出现、覆盖逐字浮现效果。
        output_lock = get_output_lock()
        async with output_lock:
            set_output_owner("command")
            set_global_state(STATE_AI_SPEAKING)
            try:
                self.tts.clear_interrupt()
                await self.tts.speak(text)
                await self.tts.drain()
            finally:
                set_output_owner(None)
                set_global_state(STATE_IDLE)
                # 说完后清字幕（前端据此 3 秒后淡出）
                if self.sub is not None:
                    self.sub.push("clear", "")

    # ---------- Mindcraft 双向桥 ----------

    async def _mindcraft_loop(self) -> None:
        """后台循环：保持与 MindServer 的连接，失败自动重试（引擎可能后启动）。"""
        bridge = self.mindcraft_bridge
        if bridge is None:
            return
        while True:
            if not bridge.connected:
                try:
                    await bridge.connect()
                    console.ok(
                        f"[Mindcraft] 已连接 MindServer（{bridge.server_url}，"
                        f"bot={bridge.agent_name}）")
                except Exception as e:
                    console.dim(f"[Mindcraft] 连接 MindServer 失败：{e}（稍后重试）")
            await asyncio.sleep(5)

    async def _on_mindcraft_bot_output(self, message: str) -> None:
        """MC bot 回复到达：走全局输出锁朗读（不打断 AI 说话）。"""
        await self.speak_bot_reply(message)

    async def speak_bot_reply(self, text: str) -> None:
        """朗读 MC bot 的回复：输出锁互斥，播报期间通知 bot 暂停自说自话。"""
        text = (text or "").strip()
        if not text:
            return
        bridge = self.mindcraft_bridge
        output_lock = get_output_lock()
        async with output_lock:
            set_output_owner("mindcraft")
            set_global_state(STATE_AI_SPEAKING)
            try:
                if bridge is not None and bridge.connected:
                    try:
                        await bridge.set_tts_playing(True)
                    except Exception:
                        pass
                console.ok(f"[MC机器人] {text}")
                if self.tts is not None:
                    self.tts.clear_interrupt()
                    await self.tts.speak(text)
                    await self.tts.drain()
                elif self.sub is not None:
                    # 无 TTS：直接推整句字幕
                    self.sub.push("text", text)
            finally:
                if bridge is not None and bridge.connected:
                    try:
                        await bridge.set_tts_playing(False)
                    except Exception:
                        pass
                set_output_owner(None)
                set_global_state(STATE_IDLE)
                # 说完后清字幕（前端据此 3 秒后淡出）
                if self.sub is not None:
                    self.sub.push("clear", "")

    # ---------- 命令分发 ----------

    def validate_cmd_path(self, raw: str) -> Optional[str]:
        """控制中心路径命令白名单：解析后必须位于项目根目录内，防目录穿越。

        返回规范化后的安全绝对路径；路径为空、越界（../ 逃逸、跨盘符）
        或非法时返回 None。项目内模型/TTS 参考音频均在项目目录下，
        正常用法不受影响。
        """
        if not raw or not raw.strip():
            return None
        try:
            base = os.path.abspath(self.cfg.PROJECT_ROOT)
            target = os.path.abspath(os.path.join(base, raw))
            if os.path.commonpath([base, target]) == base:
                return target
        except ValueError:
            # 跨盘符（如 base 在 C:、target 在 D:）commonpath 抛异常
            pass
        return None

    def build_command_registry(self) -> CommandRegistry:
        """构建本应用的命令注册表（延迟 import 避免与命令实现循环依赖）。"""
        from src.core.commands_impl import build_app_commands
        return build_app_commands(self)

    async def dispatch(self, cmd: str) -> bool:
        """命令分发（/memory / !config / !tools / !stt / !tts_* / !model 等）。

        走 CommandRegistry 按 prefix 顺序匹配；未匹配时 emotion_actor
        的 / 开兜底。返回 True 表示已消费（不进入 LLM）。
        """
        result = await self._cmd_registry.dispatch(cmd)
        if result is not None:
            return result
        # emotion_actor 命令保留在原位置（依赖具体实例）
        if self.emotion_actor is not None and cmd.startswith("/"):
            await self.emotion_actor.handle(cmd)
            return True
        return False

    # ---------- 配置热重载（细粒度） ----------

    # !config 支持的细粒度热更新组件（对应原 cmd_reload_config 各重建块）
    HOT_COMPONENTS = ("llm", "proactive", "pf", "memory", "bili", "pet", "emotion")

    async def reload_all(self) -> None:
        """全量热重载（!config 无参）：更新配置对象后按固定顺序重建全部热组件。

        顺序与原 cmd_reload_config 完全一致；任一步失败自然冒泡
        （与原全量逻辑的异常行为保持一致）。
        """
        config.reload_config()
        self.brain.reload_client()
        for name in self.HOT_COMPONENTS:
            await self._reloaders()[name]()

    async def reload_component(self, component: str) -> bool:
        """细粒度热重载单个组件（!config <组件>）。

        先更新配置对象（无副作用），再只重建指定组件；失败不抛、
        保留旧实例，返回是否成功。
        """
        reloaders = self._reloaders()
        if component not in reloaders:
            console.warn(
                f"未知的配置组件：{component}（可用：{'/'.join(self.HOT_COMPONENTS)}）")
            return False
        config.reload_config()
        try:
            await reloaders[component]()
        except Exception as e:
            console.error(f"「{component}」配置热更新失败（保留原配置）：{e}")
            return False
        return True

    def _reloaders(self) -> dict:
        """组件名 → 异步重载器（懒构建一次，供全量与细粒度共用）。"""
        if self._reloaders_map is None:
            self._reloaders_map = {
                "llm": self._reload_llm,
                "proactive": self._reload_proactive,
                "pf": self._reload_pf,
                "memory": self._reload_memory,
                "bili": self._reload_bili,
                "pet": self._reload_pet,
                "emotion": self._reload_emotion,
            }
        return self._reloaders_map

    async def _reload_llm(self) -> None:
        """LLM 客户端重建：API Key / Base URL / 模型名变更生效。"""
        self.brain.reload_client()

    async def _reload_proactive(self) -> None:
        """主动对话引擎热启停（构建成功才切换，失败保留旧实例）。"""
        cfg = self.cfg
        if cfg.PROACTIVE_ENABLED and self.proactive is None:
            self.proactive = ProactiveEngine(
                brain=self.brain, tts=self.tts, face=self.face, sub=self.sub, cfg=cfg,
                butler=self.butler if cfg.MEMORY_ENABLED else None,
                memory_manager=self.mm if cfg.MEMORY_ENABLED else None,
                profanity_filter=self.pf,
                profanity_filter_rate=cfg.PROFANITY_FILTER_RATE,
            )
            console.ok("主动对话已热启用")
        elif not cfg.PROACTIVE_ENABLED:
            self.proactive = None
            console.ok("主动对话已热关闭")

    async def _reload_pf(self) -> None:
        """内容过滤热重建。"""
        cfg = self.cfg
        if cfg.PROFANITY_FILTER_ENABLED and self.pf is None:
            self.pf = ProfanityFilter()
            console.ok("内容过滤已热启用")
        elif not cfg.PROFANITY_FILTER_ENABLED:
            self.pf = None
            console.ok("内容过滤已热关闭")

    async def _reload_memory(self) -> None:
        """记忆管家热重建。"""
        cfg = self.cfg
        if cfg.MEMORY_ENABLED and self.butler is None:
            self.butler = ButlerAgent()
            console.ok("记忆系统已热启用")
        elif not cfg.MEMORY_ENABLED:
            self.butler = None
            console.ok("记忆系统已热关闭")

    async def _reload_bili(self) -> None:
        """B 站弹幕服务热启停（先停旧服务，再按需启动新服务）。"""
        cfg = self.cfg
        was_on = self.bili_svc is not None
        room_ids = cfg.BILI_ROOM_IDS or (
            [cfg.BILI_ROOM_ID] if cfg.BILI_ROOM_ID else [])
        want_on = bool(cfg.BILI_ENABLED and room_ids)
        if was_on:
            cancel = self.stop_bili()
            self.danmaku_picker, self.bili_svc, self.danmaku_reply_task = None, None, None
            if cancel is not None:
                try:
                    await cancel
                except (asyncio.CancelledError, Exception):
                    pass
            if not want_on:
                console.ok("B 站弹幕服务已热关闭")
        if want_on:
            self.danmaku_picker, self.bili_svc, self.danmaku_reply_task = await self.start_bili()
            if self.bili_svc is not None:
                console.ok(
                    f"B 站弹幕服务已热启用（房间 {'、'.join(str(r) for r in room_ids)}）")

    async def _reload_pet(self) -> None:
        """桌宠窗口置顶/尺寸/待机动作热应用。"""
        if self.pet_widget is not None:
            self.pet_widget.apply_config(self.cfg)

    async def _reload_emotion(self) -> None:
        """情绪映射热重载（actor 常驻，映射文件变化即时生效）。"""
        if self.emotion_actor is not None:
            self.emotion_actor.load_map()
