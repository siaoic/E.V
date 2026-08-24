"""桌宠 / VTS 头像相关初始化（setup: L174-317；attach_model_change_listener: L462-506）。"""
from __future__ import annotations

import asyncio
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ev.kernel.runtime import RuntimeContext


async def setup(runtime: "RuntimeContext") -> None:
    """桌宠 pet 段（原 L174-230） + vts vtuber 模式段（原 L231-317）。"""
    from ev.utils import console
    from ev.vts.controller import VTSController
    from ev.vts.face_driver import FaceDriver
    from ev.vts.model_scanner import scan_model
    from ev.utils.subtitle_server import SubtitleServer

    cfg = runtime.cfg
    vts_ok = False
    if cfg.RUN_MODE == "pet":
        from ev.pet.widget import PetWidget, BubbleSub
        from ev.pet.driver import PetFaceDriver

        console.kv("模型", f"{cfg.LLM_MODEL}（深度思考 {'开' if cfg.LLM_THINKING else '关'}）")
        console.kv("桌宠", os.path.basename(cfg.PET_MODEL_PATH))
        console.dim("拖动模型移动位置 | 点击模型播放动作")
        if cfg.EMOTION_ACTOR_ENABLED:
            console.dim("表情/动作：Embedding 情绪自动控制已启用（用户消息分类情绪播放）")
        else:
            console.dim("表情/动作：自动情绪控制未启用")

        runtime.pet_widget = PetWidget(cfg)
        runtime.pet_widget.show()
        # 表情/动作 actor 总是创建：自动分类播放由调用点按
        # EMOTION_ACTOR_ENABLED 开关控制
        from ev.pet.emotion_actor import PetEmotionActor
        runtime.emotion_actor = PetEmotionActor(runtime.pet_widget, cfg)
        runtime.emotion_actor.scan()
        runtime.emotion_actor.load_map()
        runtime.pet_widget.on_model_loaded = lambda w: runtime.emotion_actor.scan()
        runtime.face = PetFaceDriver(runtime.pet_widget, cfg)
        runtime.pet_widget.attach_driver(runtime.face)
        runtime.face.start()
        if cfg.PET_MOTION_PATH:
            runtime.face.set_motion(cfg.PET_MOTION_PATH)
        runtime.sub = BubbleSub(runtime.pet_widget)

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
                        if runtime.pet_widget.switch_model(new_path):
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
        console.dim("键盘输入即与 E.V 对话（Ctrl+C 退出）")

        runtime.vts = VTSController()
        vts_ok = await runtime.vts.connect()
        if not vts_ok:
            console.warn("将以「纯对话」模式运行，无口型/动作/表情控制。")

        runtime.sub = SubtitleServer().start()
        console.dim(f"字幕网页：http://127.0.0.1:{runtime.sub.port}/（打字机效果，浏览器打开即可）")

    # vtuber 模式：启动时模型扫描适配
    profile = None
    if vts_ok and runtime.vts is not None:
        profile = await scan_model(runtime.vts, cfg)
        if cfg.MOUTH_PARAMETER and profile.mouth_param:
            profile.mouth_param = cfg.MOUTH_PARAMETER
        if profile.mouth_param:
            cfg.MOUTH_PARAMETER = profile.mouth_param
            cfg.MOUTH_GAIN = profile.mouth_gain

        runtime.face = FaceDriver(runtime.vts, profile)
        runtime.face.start()
        if cfg.MOTION_PATH:
            runtime.face.set_motion(cfg.MOTION_PATH)
        elif cfg.VTS_IDLE_TAKEOVER and profile.idle_motion:
            runtime.face.set_motion(profile.idle_motion)
            console.ok(f"待机动画接管：{os.path.basename(profile.idle_motion)}"
                      f"（P2 覆盖 VTS 待机，循环点已平滑）")

        # 表情/动作演员（VTS 模式）：用户消息 → Embedding 情绪分类 → 播放表情/动作。
        # 总是创建：手动命令与控制中心试播不依赖自动控制开关，
        # 自动分类播放由主循环按 EMOTION_ACTOR_ENABLED 开关控制
        from ev.vts.emotion_actor import VtsEmotionActor
        runtime.emotion_actor = VtsEmotionActor(runtime.vts, cfg, face=runtime.face)
        await runtime.emotion_actor.scan()
        runtime.emotion_actor.load_map()
        if cfg.EMOTION_ACTOR_ENABLED:
            console.dim("表情/动作：Embedding 情绪自动控制已启用（用户消息分类情绪播放）")
        else:
            console.dim("表情/动作：自动情绪控制未启用")

    # Embedding 预热：情绪分类器是惰性初始化（第一条用户消息才构建 provider
    # 并对语料批量向量化），启动后台提前构建，避免首轮消息的冷启动延迟。
    # 与 TTS / LLM 连接预热同一模式：失败静默，不影响启动。
    actor = getattr(runtime, "emotion_actor", None)
    if actor is not None and cfg.EMOTION_ACTOR_ENABLED:
        async def _warm_emotion_classifier() -> None:
            try:
                await actor.initialize()
            except Exception:
                pass
        asyncio.create_task(_warm_emotion_classifier())

    # 把 vts_ok 暂存到 runtime 上，供 attach_model_change_listener 使用
    runtime._vts_ok_flag = vts_ok  # type: ignore[attr-defined]


async def attach_model_change_listener(runtime: "RuntimeContext") -> None:
    """原 setup 结尾 L462-506：VTS model change listener。"""
    from ev.utils import console
    from ev.vts.model_scanner import scan_model

    vts_ok = bool(getattr(runtime, "_vts_ok_flag", False))
    if not vts_ok or runtime.vts is None:
        return
    _scanning = False

    async def _rescan_on_model_switch(msg: dict) -> None:
        nonlocal _scanning
        if not msg.get("data", {}).get("modelLoaded") or _scanning:
            return
        console.info("检测到模型切换，暂停伪面捕并重新扫描适配...")
        _scanning = True
        try:
            await runtime.face.stop()
            new_profile = await scan_model(runtime.vts, runtime.cfg)
            if not new_profile.model_name:
                console.warn("新模型未加载，跳过适配（保留当前状态）")
                return
            if runtime.cfg.MOUTH_PARAMETER and new_profile.mouth_param:
                new_profile.mouth_param = runtime.cfg.MOUTH_PARAMETER
            if new_profile.mouth_param:
                runtime.cfg.MOUTH_PARAMETER = new_profile.mouth_param
                runtime.cfg.MOUTH_GAIN = new_profile.mouth_gain
            runtime.face.apply_profile(new_profile)
            console.ok(f"已适配新模型「{new_profile.model_name}」")
            if runtime.cfg.MOTION_PATH:
                runtime.face.set_motion(runtime.cfg.MOTION_PATH)
            elif runtime.cfg.VTS_IDLE_TAKEOVER:
                if new_profile.idle_motion:
                    runtime.face.set_motion(new_profile.idle_motion)
                    console.ok(f"待机动画接管：{os.path.basename(new_profile.idle_motion)}")
                else:
                    # 新模型无待机动画可接管：停止旧模型遗留的动作注入
                    runtime.face.stop_motion()
            # 表情/动作演员重扫（新模型的表情/动画热键不同）
            if runtime.emotion_actor is not None:
                try:
                    await runtime.emotion_actor.scan()
                except Exception as e:
                    console.dim(f"表情/动作重扫失败：{e}")
        finally:
            _scanning = False
            runtime.face.start()

    runtime.vts.on_event("ModelLoadedEvent", _rescan_on_model_switch)
    if await runtime.vts.subscribe_event("ModelLoadedEvent"):
        console.dim("已订阅模型切换事件：运行中切换模型将自动重新适配")


async def teardown(runtime: "RuntimeContext") -> None:
    """原 teardown 的 avatar/VTS/face/字幕相关清理段（逆序）。"""
    import asyncio

    if runtime.sub is not None and hasattr(runtime.sub, "stop"):
        try:
            runtime.sub.stop()
        except Exception:
            pass
    if runtime.face is not None:
        try:
            await asyncio.wait_for(runtime.face.stop(), timeout=10)
        except Exception:
            pass
    if runtime.vts is not None:
        try:
            await asyncio.wait_for(runtime.vts.close(), timeout=10)
        except Exception:
            pass
