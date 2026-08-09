"""AI 全权接管的面部驱动（伪面捕）：持续注入 VTS tracking 参数。

职责范围：
- 口型同步（说话时按 RMS 能量曲线实时开合嘴巴，真正动态口型）
- 自动眨眼（替代模型 idle 眨眼）
- 动作文件基线播放（.motion3.json 驱动头部/眉毛/呼吸等）

关键修复：
- LipsyncScheduler 已删除，其独立协程与 FaceDriver._loop() 互相覆盖参数，
  导致嘴只开到固定值。RMS 曲线播放直接集成在 _loop() 中，不再冲突。
- 根据 VTS API 官方要求：注入参数需至少每秒刷新一次，否则参数回退默认。
- 说话时有曲线用曲线，无曲线时用节拍回退模式。
- AI 不再控制模型参数（ActionController 已移除）：表情只由动作文件基线
  + 自动眨眼 + 口型同步驱动。"""

import asyncio
import math
import random
import time
from typing import Dict, List, Optional, Tuple

from src.utils import console
from src.vts.lipsync import compute_rms_curve
from src.vts.model_scanner import ModelProfile
from src.vts.motion_player import MotionPlayer
from src.vts.controller import VTSController

# 帧间隔（秒）≈ 30fps
FRAME_S = 0.033
# 说话时的张嘴候选参数（模型扫描探测不到时回退使用；探测成功只注入探测结果）。
# 注意：MouthSmile 是「嘴型形状」而非「张嘴开合」，单独由嘴角逻辑控制，
# 不再与张嘴参数混用同一数值（否则模型同时映射两者时嘴会过度张开）。
_MOUTH_PARAMS = ("VoiceVolumePlusMouthOpen", "MouthOpen")
# 沉默时注入 MouthSmile 中性点（用于反相映射模型，见 model_scanner）
# 默认 0.50 → hiyori 类 ParamMouthForm = -1.0 + 2×MouthSmile 时闭合

# 眨眼闭眼比例时间轴（0.04s/帧，1=全闭 0=全开）：快闭 0.16s → 渐开 0.12s
BLINK_CURVE = [0.0, 1.0, 1.0, 1.0, 0.6, 0.3, 0.0]
# 默认眨眼参数（扫描结果优先；无扫描时用标准 tracking 名）
_DEFAULT_EYES = ("EyeOpenLeft", "EyeOpenRight")

# 节拍回退模式参数（无 RMS 曲线时使用）
_BEAT_OPEN = 0.65      # 节拍峰值
_BEAT_CLOSE = 0.08     # 节拍谷值
# 节拍频率（rad/s）与权重：~2.4Hz + 3.7Hz 叠加 + 1.4Hz 包络，
# 模拟 3~5 音节/秒的真实说话节奏（原 1.2/1.9Hz 太慢，看起来像周期性张嘴）
_BEAT_FREQS = (15.0, 23.0, 9.0)
_BEAT_WEIGHTS = (0.55, 0.30, 0.15)
# 说话微颤：即使能量平稳，口型也保持细微信号起伏（防"僵在半张"）
_TREMOLO = 0.08
_TREMOLO_FREQ = 14.0

# 口型平滑包络（每帧插值系数）：attack 快开、release 慢合，模仿嘴部肌肉。
# 真实口型：音爆瞬间嘴快速张开，收口时缓慢闭合。直接映射 RMS 会帧间抖跳。
_MOUTH_ATTACK = 0.85   # 开嘴速度（~1.5 帧到位 ≈ 50ms，音节起头瞬时张开）
_MOUTH_RELEASE = 0.55  # 闭嘴速度（~2.5 帧闭合 ≈ 80ms，音节间隙快速收口）。
                       # 音节间隙常只有 80~120ms（2-3 帧），release 太慢会让嘴
                       # 悬在半张——正是"只像张嘴"的根源，必须跟得上音节节奏
# 说话时嘴的最小开合度（避免语音弱段嘴完全闭合的僵硬感）。
# 0.05 比 0.12 更低：弱音段嘴更贴近闭合，开合动态更明显
_MOUTH_MIN_OPEN = 0.05
# 说话时嘴角上扬幅度（MouthSmile 从中性点向上偏移，形成「说话嘴型」）
_MOUTH_SMILE_LIFT = 0.12


class FaceDriver:
    """后台常驻注入循环：口型同步 + 自动眨眼 + 动作文件基线。"""

    def __init__(self, vts: VTSController,
                 profile: Optional[ModelProfile] = None) -> None:
        self.vts = vts
        self.profile = profile or ModelProfile()
        self._task: Optional[asyncio.Task] = None
        self._params: Dict[str, float] = {}

        # —— 口型同步 ——
        self._mouth_gain: float = self.profile.mouth_gain
        self._speaking_until: float = 0.0
        # 张嘴参数：优先模型探测结果；探测失败回退全候选（VTS 跳过不支持的）
        self._mouth_param_ids: Tuple[str, ...] = self._resolve_mouth_params()
        # MouthSmile 闭合中性点（反相映射模型嘴自然闭合所需值）
        self._mouth_form_neutral: Optional[float] = self.profile.mouth_form_neutral

        # RMS 曲线播放状态
        self._rms_curve: Optional[List[float]] = None   # 当前句子曲线
        self._rms_curve_start: float = 0.0              # 曲线播放开始时间
        self._rms_frame_ms: int = 40                    # 曲线帧间隔（毫秒）
        self._rms_duration_ms: int = 0                  # 曲线总时长（毫秒）
        # 当前口型开合度（0~1 平滑状态，attack/release 包络）
        self._mouth_level: float = 0.0

        # —— 眨眼 ——
        self._eye_param_ids: Optional[Tuple[str, str]] = self.profile.eyes or _DEFAULT_EYES
        self._blink_phase: Optional[int] = None
        self._next_blink: float = random.uniform(2.0, 5.0)
        self._t: float = 0.0

        # —— 动作文件播放器 ——
        self._motion: Optional[MotionPlayer] = None

    # ---------- 生命周期 ----------

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        # 复位参数
        reset = {"FaceAngleY": 0.0, "FaceAngleZ": 0.0}
        for pid in self._mouth_param_ids:
            reset[pid] = 0.0
        if self._eye_param_ids:
            for p in self._eye_param_ids:
                reset[p] = 0.0
        await self._inject(reset)
        self._params = {}

    def apply_profile(self, profile: ModelProfile) -> None:
        """热更新模型适配结果。"""
        self.profile = profile
        self._mouth_gain = profile.mouth_gain
        self._mouth_param_ids = self._resolve_mouth_params()
        self._mouth_form_neutral = profile.mouth_form_neutral
        self._eye_param_ids = profile.eyes or _DEFAULT_EYES

    def _resolve_mouth_params(self) -> Tuple[str, ...]:
        """决定注入哪些「张嘴」参数。

        优先使用模型扫描探测出的唯一嘴部驱动参数（避免多个参数同时映射
        张嘴导致叠加过头）；探测失败时回退全部候选（VTS 自动忽略不支持的）。
        """
        p = self.profile.mouth_param
        if p:
            return (p,)
        return _MOUTH_PARAMS

    # ---------- 注入目标抽象 ----------
    # 默认目标为 VTubeStudio；桌宠模式（vtuber/src/pet/driver.py 的 PetFaceDriver）
    # 继承本类并重写这两个方法，把同一套口型/眨眼/节拍逻辑驱动到本地模型。
    # 这样 stream.py / proactive.py / main.py 无需感知渲染目标。

    async def _ensure_connected(self) -> bool:
        """确认注入目标可用（VTS：连接就绪；桌宠：本地模型已加载）。"""
        return await self.vts.ensure_connected()

    async def _inject(self, params: Dict[str, float]) -> None:
        """向注入目标发送一批参数。"""
        await self.vts.inject_parameters(params)

    def _motion_frame(self) -> Dict[str, float]:
        """取当前基线动作帧（已剔除嘴部参数）。

        VTS 模式返回映射为 VTS 跟踪参数名的帧；桌宠模式（PetFaceDriver）
        重写本方法返回 Cubism 原生参数帧（保留 ParamBreath 等呼吸参数）。
        """
        if not (self._motion and self._motion.active):
            return {}
        frame = self._motion.get_frame()
        frame.pop("MouthSmile", None)
        frame.pop("MouthOpen", None)
        return frame

    # ---------- 口型同步控制 ----------

    def start_speaking(self, duration: float) -> None:
        """标记开始说话。曲线加载通过 load_speech_curve（由 TTS 播放回调触发）。

        无曲线时 _loop() 自动使用节拍回退模式，等到 TTS 播放时
        回调 load_speech_curve 后平滑切换为 RMS 曲线。
        """
        self._speaking_until = time.time() + max(0.5, duration)

    def load_speech_curve(self, wav_path: str) -> bool:
        """由 TTS 播放回调调用：加载 RMS 曲线覆盖当前说话会话的口型。

        同时根据曲线实际时长更新 _speaking_until，确保口型同步
        与音频播放时长一致（含 0.3s 自然衰减缓冲）。

        Returns:
            True = 曲线加载成功（口型随曲线运动）；
            False = 分析失败（调用方应改用 start_speaking(dur) 回退节拍口型）。
        """
        curve, dur_ms, frame_ms = compute_rms_curve(wav_path)
        if curve is not None:
            self._rms_curve = curve
            self._rms_duration_ms = dur_ms
            self._rms_frame_ms = frame_ms if frame_ms > 0 else 40
            self._rms_curve_start = time.time()
            # 用实际音频时长覆盖估算值，+0.3s 缓冲防止口型提前闭合
            self._speaking_until = time.time() + (dur_ms / 1000.0) + 0.3
            return True
        return False

    def stop_speaking(self) -> None:
        self._speaking_until = 0.0
        self._rms_curve = None

    # ---------- 动作文件播放 ----------

    def set_motion(self, path: str) -> None:
        import os
        if not os.path.isfile(path):
            console.warn(f"动作文件不存在：{path}")
            return
        # 幂等：同一动作文件且正在播放时跳过（桌宠待机循环每帧都会调用）
        if (self._motion is not None and self._motion.active
                and getattr(self._motion, "path", None) == path):
            return
        try:
            self._motion = MotionPlayer(path)
            self._motion.start()
            console.info(f"动作文件已加载并开始播放："
                         f"{os.path.basename(path)}"
                         f"（{self._motion._duration:.1f}s，"
                         f"{'循环' if self._motion._loop else '一次'}）")
        except Exception as e:
            console.warn(f"加载动作文件失败：{e}")
            self._motion = None

    def stop_motion(self) -> None:
        if self._motion:
            self._motion.stop()
            self._motion = None
        console.info("动作文件已停止")

    # ---------- 核心循环 ----------

    async def _loop(self) -> None:
        """主注入循环（≈30fps）。

        每帧注入分两路（VTS 对批量注入支持不稳定，拆开保证口型可靠）：
        - 口型参数：**单独请求**注入（历史上逐参数注入实测有效；
          批量注入时 VTS 可能忽略整批，导致口型失效）
        - 动效 + 眨眼：批量注入（被忽略只影响动画，不影响口型）
        """
        try:
            while True:
                # 连接不可用（VTS 断开/插件异常）→ 降频到 1Hz 等待自动重连，
                # 避免对死连接按 30fps 反复注入刷屏
                if not await self._ensure_connected():
                    self._t += 1.0
                    await asyncio.sleep(1.0)
                    continue

                params: Dict[str, float] = {}          # 动效 + 眨眼（批量）
                mouth_payload: Dict[str, float] = {}   # 口型（单独注入）

                # ---------- 1) 动作文件基线 ----------
                # 剔除嘴部参数（ParamMouthForm→MouthSmile / ParamMouthOpenY→MouthOpen）：
                # 嘴全权由口型同步控制，动效写 Mouth 只会与口型打架
                params.update(self._motion_frame())

                # ---------- 2) 口型同步 ----------
                now = time.time()
                if now < self._speaking_until:
                    # —— 说话中：先求目标开合度 ——
                    if self._rms_curve is not None:
                        # 使用 RMS 曲线：真正动态口型
                        elapsed_ms = (now - self._rms_curve_start) * 1000
                        idx = int(elapsed_ms / self._rms_frame_ms)
                        if idx < len(self._rms_curve):
                            target = self._rms_curve[idx]
                        else:
                            target = 0.0  # 曲线播完 → 闭嘴（由 release 平滑收拢）
                        # 说话微颤：长音/平稳段口型保持细小起伏，避免嘴僵在一个开度
                        target = min(1.0, target * (1.0 - _TREMOLO
                                                    + _TREMOLO * math.sin(now * _TREMOLO_FREQ)))
                    else:
                        # 无曲线：不规则节拍回退——多频正弦叠加（相位不同步），
                        # 频率按说话节奏（~3-5 音节/秒），比单一正弦更接近含糊说话
                        wave = sum(w * math.sin(now * f)
                                   for f, w in zip(_BEAT_FREQS, _BEAT_WEIGHTS))
                        beat = (wave + 1.0) * 0.5  # 0~1
                        target = _BEAT_CLOSE + (_BEAT_OPEN - _BEAT_CLOSE) * (beat ** 2)

                    # —— 口型包络：attack 快开 / release 慢合 ——
                    # 消除帧间抖跳，音爆瞬间快速张开、收口缓慢闭合
                    if target > self._mouth_level:
                        self._mouth_level += (target - self._mouth_level) * _MOUTH_ATTACK
                    else:
                        self._mouth_level += (target - self._mouth_level) * _MOUTH_RELEASE

                    # —— 张嘴：最小开合度映射 + gain ——
                    mouth = _MOUTH_MIN_OPEN + self._mouth_level * (1.0 - _MOUTH_MIN_OPEN)
                    mouth = min(1.0, mouth * self._mouth_gain)
                    for pid in self._mouth_param_ids:
                        mouth_payload[pid] = mouth

                    # —— 嘴角（MouthSmile）：说话时轻微上扬，形成「说话嘴型」——
                    # 仅当张嘴参数本身不是 MouthSmile 时做形状偏移，避免数值叠加
                    if (self._mouth_form_neutral is not None
                            and "MouthSmile" not in self._mouth_param_ids):
                        mouth_payload["MouthSmile"] = min(
                            1.0, self._mouth_form_neutral
                            + self._mouth_level * _MOUTH_SMILE_LIFT)
                else:
                    # —— 沉默：口型按 release 平滑闭合，嘴角回中性点 ——
                    # 关键：静默期也要持续注入开合值（随 _mouth_level 衰减到 0）。
                    # 若静默期不注入，VTS 会维持上次说话的开度直到超时复位，
                    # 造成「没说话嘴也张着」。
                    self._mouth_level *= (1.0 - _MOUTH_RELEASE)
                    if self._mouth_level < 0.01:
                        self._mouth_level = 0.0
                    mouth = min(1.0, self._mouth_level * self._mouth_gain)
                    for pid in self._mouth_param_ids:
                        mouth_payload[pid] = mouth
                    if self._mouth_form_neutral is not None:
                        mouth_payload["MouthSmile"] = self._mouth_form_neutral

                # ---------- 3) 自动眨眼 ----------
                if self._eye_param_ids:
                    if (self._blink_phase is None
                            and self._t >= self._next_blink):
                        self._blink_phase = 0
                        self._next_blink = self._t + random.uniform(2.0, 6.0)
                    closing = (self._blink_phase is not None
                               and self._blink_phase < len(BLINK_CURVE))
                    eye_close = BLINK_CURVE[self._blink_phase] if closing else 0.0
                    eye_open = 1.0 - eye_close
                    for pid in self._eye_param_ids:
                        params[pid] = eye_open
                    if self._blink_phase is not None:
                        self._blink_phase += 1
                        if self._blink_phase >= len(BLINK_CURVE) + 3:
                            self._blink_phase = None

                # —— 注入：口型单独（可靠路径）+ 动效/眨眼批量 ——
                if mouth_payload:
                    await self._inject(mouth_payload)
                if params:
                    await self._inject(params)
                self._t += FRAME_S
                await asyncio.sleep(FRAME_S)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            console.error(f"FaceDriver 注入循环异常："
                          f"{type(e).__name__}: {e}")
            raise