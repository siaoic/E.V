"""口型同步：分析 TTS 音频的 RMS 能量曲线。

- `compute_rms_curve`: 用 pydub 解码音频（mp3/wav）→ mono → 分帧 RMS →
  噪音地板切除 → 移动平均平滑 → 归一化到 [0,1]（带灵敏度压缩）。
  返回 (curve, duration_ms, frame_ms)。解码失败返回 (None, duration_ms, 0)。
- FaceDriver 直接集成曲线播放，不再使用独立的 LipsyncScheduler（已被删除，
  因为其独立协程与 FaceDriver._loop() 互相覆盖注入参数，导致嘴型只开到固定值）。

曲线预处理（让口型更像说话）：
1. 噪音地板切除：低于峰值 10% 的静音段置 0（闭嘴），避免背景噪音让嘴微张
2. 移动平均平滑：消除帧间能量抖动，口型过渡更顺滑
3. 音节起点强调：每个音节开头的能量突增推高、间隙压低，口型随音节「张开→收拢」
   （纯能量归一化后 TTS 语音大部分帧处于中高能量区间，直接驱动会"恒半张"）
4. 分位数归一化 [p10, p90]：能量再平也能拉开层次，避免嘴恒在半张
5. 对比度拉伸：把中间值拉开，开/合区分更清晰
"""

from typing import List, Optional, Tuple

import numpy as np

from src.utils import console

# 噪音地板：低于峰值该比例的 RMS 视为静音（置 0）。
# 0.10 比默认 2% 更积极——把弱音段也当静音，让"静→说"的开合对比更明显
_NOISE_FLOOR = 0.10
# 平滑窗口（帧）：奇数，1=不平滑。3 帧保留更多原始动态（5 帧会抹平开合起伏）
_SMOOTH_WINDOW = 3
# 音节起点强调系数：v' = v + k×(v - 局部均值)。k 越大音节峰值越尖、间隙越凹，
# 嘴按音节「张开→收拢」快速交替，避免整体平缓导致「恒半张像张嘴」
_ONSET_EMPHASIS = 2.0
# 对比度拉伸倍数（把中间值拉开，开/合区分更清晰）
_CONTRAST = 1.6


def compute_rms_curve(audio_path: str,
                      frame_ms: int = 40) -> Tuple[Optional[List[float]], int, int]:
    """计算音频的 RMS 能量曲线。

    Args:
        audio_path: WAV/MP3 文件路径。
        frame_ms: 每帧时长（毫秒），默认 40ms（25fps）。

    Returns:
        (curve, duration_ms, frame_ms)：
            curve: 0~1 归一化能量列表（经噪音切除+平滑+压缩），None 表示分析失败。
            duration_ms: 音频总时长（毫秒）。
            frame_ms: 实际帧间隔（毫秒）。
        解码失败返回 (None, duration_ms, frame_ms)。
    """
    try:
        from pydub import AudioSegment
        audio = AudioSegment.from_file(audio_path)
        audio = audio.set_channels(1).set_frame_rate(16000)
        duration_ms = len(audio)

        samples = np.array(audio.get_array_of_samples(), dtype=np.float32)
        # 按位深归一化
        max_amp = float(2 ** (8 * audio.sample_width - 1))
        if max_amp > 0:
            samples /= max_amp

        frame_samples = int(16000 * frame_ms / 1000)
        if frame_samples <= 0:
            return None, duration_ms, frame_ms

        n_frames = len(samples) // frame_samples
        if n_frames == 0:
            return None, duration_ms, frame_ms

        curve: List[float] = []
        for i in range(n_frames):
            frame = samples[i * frame_samples:(i + 1) * frame_samples]
            rms = float(np.sqrt(np.mean(frame ** 2))) if frame.size else 0.0
            curve.append(rms)

        if not curve:
            return None, duration_ms, frame_ms

        # ---------- 预处理 ----------
        # 1) 归一化 + 噪音地板切除（静音段闭嘴）
        max_v = max(curve) or 1.0
        floor = max_v * _NOISE_FLOOR
        span = max_v - floor
        curve = [max(0.0, (v - floor) / span) for v in curve]

        # 2) 移动平均平滑（消除帧间抖动）
        arr = np.array(curve, dtype=np.float32)
        if _SMOOTH_WINDOW > 1 and len(arr) >= _SMOOTH_WINDOW:
            half = _SMOOTH_WINDOW // 2
            kernel = np.ones(_SMOOTH_WINDOW) / _SMOOTH_WINDOW
            # 边界用最近值填充（不缩短曲线长度）
            arr_pad = np.pad(arr, half, mode="edge")
            arr = np.convolve(arr_pad, kernel, mode="valid")

        # 3) 音节起点强调（边缘增强）：
        #    纯能量归一化后，TTS 语音大部分帧都处于中高能量区间，直接驱动
        #    会让嘴"恒在半张、只有缓慢起伏"，看起来像张嘴不像说话。
        #    这里做高通：v' = v + k×(v - 局部均值)，音节开头的能量突增被推高、
        #    末尾的骤降被压低，嘴随音节「张开→收拢」快速交替，出现说话感。
        if len(arr) >= 7:
            lp_kernel = np.ones(5) / 5.0
            lp = np.convolve(np.pad(arr, 2, mode="edge"), lp_kernel, mode="valid")
            arr = arr + _ONSET_EMPHASIS * (arr - lp)

        # 4) 分位数归一化：把能量分布拉开，避免"整体高位"导致的嘴恒张。
        #    用 [p10, p90] 拉伸：无论音频多响，总有 10% 帧贴近闭合、
        #    10% 帧接近全开，开合层次始终分明。
        p10, p90 = np.percentile(arr, [10.0, 90.0])
        if p90 - p10 > 1e-3:
            arr = (arr - p10) / (p90 - p10)
        arr = np.clip(arr, 0.0, 1.0)

        # 5) 对比度拉伸：把中间值拉开，嘴的开/合区分更清晰。
        #    让 <0.4 压向闭合、>0.6 压向张开，动态层次更分明
        arr = 0.5 + (arr - 0.5) * _CONTRAST
        arr = np.clip(arr, 0.0, 1.0)
        curve = [float(x) for x in arr]
        return curve, duration_ms, frame_ms
    except Exception as e:
        # 估算时长用于降级循环
        try:
            from pydub.utils import mediainfo
            duration_ms = int(float(mediainfo(audio_path).get("duration", 0)) * 1000)
        except Exception:
            duration_ms = 0
        console.warn(f"音频分析失败（可能缺 ffmpeg），将使用简化口型：{e}")
        return None, max(duration_ms, 800), frame_ms