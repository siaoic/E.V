"""VAD 情绪状态机（精简移植自 Soullink Emotion 引擎）。

参考：e:\\AI\\vtuber\\情绪\\packages\\engine\\src\\emotion\\
- EmotionPresetRegistry.ts   预设情绪的 VAD 坐标（valence/arousal/dominance ∈ [-1,1]）
- EmotionStateController.ts  nudge 推目标 + 指数逼近 + 自然衰减回落

情绪用连续三维空间表示，而不是离散标签硬切：
- valence 效价：消极(-1) ↔ 积极(+1)
- arousal 唤醒：平静/疲惫(-1) ↔ 兴奋/紧张(+1)
- dominance 支配：退缩/顺从(-1) ↔ 自信/主动(+1)

核心机制（对应「情绪到达 → 停留 → 自然回落」）：
1. nudge(name, intensity)：情绪意图到达 → 把 target 向预设点推近（幅度随强度增大），并保持一段时间
2. update(dt)：current 以指数速度逼近 target；保持期结束后 target 向 baseline 自然衰减
3. dominant_emotion()：对 current 做加权欧氏距离，推断当前主情绪
"""

import math
from typing import Dict, Optional, Tuple

# VAD 预设表（valence, arousal, dominance），键为中文情绪名
# 6 个基础情绪，与控制中心「情绪与动作」页 EMOTIONS 保持一致
VAD_PRESETS: Dict[str, Tuple[float, float, float]] = {
    "开心": (0.75, 0.45, 0.35),     # happy
    "生气": (-0.70, 0.75, 0.55),    # anger
    "疑惑": (-0.10, 0.35, -0.30),   # confused
    "悲伤": (-0.65, -0.45, -0.50),  # sad
    "害怕": (-0.60, 0.70, -0.55),   # fear（高唤醒 + 退缩，与「生气」的支配区分）
    "厌恶": (-0.50, 0.30, -0.20),   # disgust
}

# 主情绪推断的加权距离权重（参考 VADExpressionMapper）
_V_W, _A_W, _D_W = 1.08, 0.88, 1.28
# 距离超过该值视为无明显情绪（回到中性）
_MAX_PRESET_DIST = 0.92


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def get_vad_preset(name: str,
                   variant: Optional[str] = None) -> Optional[Tuple[float, float, float]]:
    """按情绪名 + variant 取 VAD 预设（对齐 Soullink getVADPreset）。

    variant 特化情绪（害羞/担忧/惊讶）不在 6 基础情绪表中时回退原预设；
    识别失败返回 None。
    """
    preset = VAD_PRESETS.get(name)
    if preset is None:
        return None
    if variant:
        if "shy" in variant:
            preset = VAD_PRESETS.get("害羞", preset)
        elif "comfort" in variant:
            preset = VAD_PRESETS.get("担忧", preset)
        elif "startled" in variant:
            preset = VAD_PRESETS.get("惊讶", preset)
    return preset


class VADState:
    """连续情绪状态：target 由情绪意图推动，current 平滑跟随，情绪自然衰减回落。"""

    def __init__(self, baseline: Tuple[float, float, float] = (0.0, 0.0, 0.0),
                 approach_rate: float = 1.35, decay_rate: float = 0.05,
                 hold_base: float = 18.0, reactivity: float = 1.0) -> None:
        self.baseline = tuple(baseline)
        self.current: list = list(baseline)
        self.target: list = list(baseline)
        self._approach_rate = approach_rate   # current 追 target 的速度
        self._decay_rate = decay_rate         # 保持期后 target 回落 baseline 的速度
        self._hold_base = hold_base           # 情绪保持时长基数（秒）
        self._reactivity = reactivity         # 情绪敏感度
        self._hold = 0.0                      # 剩余保持时间（秒）
        self.last_emotion: Optional[str] = None

    @staticmethod
    def _lerp(a: float, b: float, t: float) -> float:
        return a + (b - a) * t

    # ---------- 情绪意图 ----------

    def nudge(self, name: str, intensity: float = 0.8,
              variant: Optional[str] = None) -> bool:
        """情绪意图到达：把 target 推近对应预设点并保持一段时间。识别失败返回 False。

        variant 微调预设（见 get_vad_preset：特化情绪不在 6 基础表中时回退原预设）。
        """
        preset = get_vad_preset(name, variant)
        if preset is None:
            return False
        self._push_target(preset, name, intensity)
        return True

    def nudge_vad(self, name: str, vad: Tuple[float, float, float],
                  intensity: float = 0.8) -> bool:
        """直接设置 VAD 目标（Embedding 分类器给出连续 naturalVAD 时使用，
        对齐 triggerIntent 的 vadTarget 语义：target = 连续 VAD 坐标）。"""
        self.target = [self._clamp_component(v) for v in vad]
        self._hold = 6.0 + intensity * self._hold_base
        self.last_emotion = name
        return True

    @staticmethod
    def _clamp_component(v: float) -> float:
        return max(-1.0, min(1.0, v))

    def _push_target(self, preset: Tuple[float, float, float],
                     name: str, intensity: float) -> None:
        """把 target 向 preset 推近，幅度随强度增大，并保持一段时间。"""
        intensity = _clamp(intensity, 0.0, 1.0)
        amount = _clamp((0.35 + intensity * 0.65) * self._reactivity, 0.0, 1.0)
        self.target = [self._lerp(t, p, amount) for t, p in zip(self.target, preset)]
        self._hold = 6.0 + intensity * self._hold_base
        self.last_emotion = name

    def blend_to(self, vad: Tuple[float, float, float], amount: float = 0.65) -> None:
        """直接向任意 VAD 坐标混合（LLM 给连续 VAD 时使用）。"""
        self.target = [self._lerp(t, p, amount) for t, p in zip(self.target, vad)]
        self._hold = max(self._hold, 6.0)

    def reset(self) -> None:
        self.current = list(self.baseline)
        self.target = list(self.baseline)
        self._hold = 0.0
        self.last_emotion = None

    # ---------- 每帧更新 ----------

    def update(self, dt: float) -> None:
        if self._hold > 0:
            self._hold -= dt
        approach = 1.0 - math.exp(-dt * self._approach_rate)
        self.current = [self._lerp(c, t, approach)
                        for c, t in zip(self.current, self.target)]
        if self._hold <= 0:
            decay = 1.0 - math.exp(-dt * self._decay_rate)
            self.target = [self._lerp(t, b, decay)
                           for t, b in zip(self.target, self.baseline)]

    # ---------- 读取 ----------

    def dominant_emotion(self, default: str = "中性") -> str:
        """按加权欧氏距离推断主情绪；无明显情绪时返回 default。

        基于 target（情绪意图 + 衰减后的目标）而非 current：
        target 稳定反映「当前想要的表情」，避免追赶过程造成情绪误判；
        实际表情幅度仍由 intensity()（current 幅值）控制。
        """
        v, a, d = self.target
        best, best_d = default, float("inf")
        for name, (pv, pa, pd) in VAD_PRESETS.items():
            dist = math.sqrt(_V_W * (v - pv) ** 2
                             + _A_W * (a - pa) ** 2
                             + _D_W * (d - pd) ** 2)
            if dist < best_d:
                best, best_d = name, dist
        if best_d > _MAX_PRESET_DIST:
            return default
        return best

    def intensity(self) -> float:
        """当前情绪强度（幅值，0~1）。"""
        v, a, d = self.current
        return _clamp((abs(v) + abs(a) * 0.82 + abs(d) * 0.64) / 2.46, 0.0, 1.0)

    def vector(self) -> Tuple[float, float, float]:
        return tuple(self.current)