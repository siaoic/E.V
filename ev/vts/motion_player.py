"""MotionPlayer: 解析 .motion3.json 动作文件并逐帧提供参数值。

支持两种模式：
1. Hotkey 模式（首选）— 通过 VTS HotkeyTriggerRequest 触发原生动画播放
2. 直接注入模式（回退）— 逐帧解析动作曲线并注入参数值

参数名映射（Live2D 模型内部参数 → VTS 跟踪参数）：
  - ParamAngleX → FaceAngleX
  - ParamBrowLY → BrowLeftY
  - ParamEyeLOpen → EyeOpenLeft
  - 等
"""

import json
import os
import time
from typing import Dict, List, Optional, Set, Tuple

from ev.utils import console

# Live2D 模型内部参数 → VTS 跟踪参数 映射
_PARAM_MAP: Dict[str, str] = {
    "ParamAngleX": "FaceAngleX",
    "ParamAngleY": "FaceAngleY",
    "ParamAngleZ": "FaceAngleZ",
    "ParamBrowLY": "BrowLeftY",
    "ParamBrowRY": "BrowRightY",
    "ParamBrowLForm": "BrowLeftX",
    "ParamBrowRForm": "BrowRightX",
    "ParamEyeLOpen": "EyeOpenLeft",
    "ParamEyeROpen": "EyeOpenRight",
    "ParamMouthForm": "MouthSmile",
    "ParamMouthOpenY": "MouthOpen",
}

# 需要展开为多参数的映射（如 ParamEyeBallX → EyeLeftX + EyeRightX）
_PARAM_EXPAND: Dict[str, List[str]] = {
    "ParamEyeBallX": ["EyeLeftX", "EyeRightX"],
    "ParamEyeBallY": ["EyeLeftY", "EyeRightY"],
}

# 循环播放时尾帧→首帧的平滑过渡时长（秒），可用 .env MOTION_LOOP_BLEND_SECONDS 调整。
# .motion3.json 首尾帧参数值通常不同（动画并未为无缝循环设计），
# 直接取模会在结尾瞬间突跳到首帧值。在最后这段窗口内把尾帧值平滑混合到首帧值，
# 让动作自然"回到原点"。默认 0.5s；跳变幅度大的动画可调大（上限 2s）。
_LOOP_BLEND_WINDOW = min(2.0, max(0.1,
                                  float(os.getenv("MOTION_LOOP_BLEND_SECONDS", "0.5"))))
# 首尾帧判定为"无缝循环"的允许误差：误差内不混合（混合会扭曲本就连续的循环）
_LOOP_SEAMLESS_EPS = 1e-3

# 无缝化备份后缀：原文件改前备份为 <file>.bak（仅首次）
_SEAMLESS_BACKUP_SUFFIX = ".bak"


def make_seamless(path: str, blend: Optional[float] = None) -> bool:
    """将待机动画 .motion3.json 改造为无缝循环（改前自动备份 <file>.bak）。

    Live2D 动画默认首尾帧不衔接：VTS 原生循环到尾帧后硬跳回首帧，
    待机动作每循环一次就「弹回」一次。本函数在每条首尾值不一致的曲线
    末尾追加一段 smoothstep 过渡（尾帧值 → 首帧值），并延长 Meta.Duration，
    使动画自身首尾连续——VTS 原生播放即为无缝循环（覆盖全部参数，含
    插件无法注入的 Live2D 自定义参数，如 Param156、Xbox_* 等）。

    - 仅追加到首尾不一致的曲线；首尾一致的曲线保持原样（循环点本就连续）
    - 首次修改前把原文件备份为 <path>.bak；文件已无缝时幂等跳过
    - 任一步失败返回 False（调用方继续按原文件播放，不影响流程）
    """
    blend = _LOOP_BLEND_WINDOW if blend is None else blend
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        console.dim(f"无缝化：读取 {os.path.basename(path)} 失败（{e}）")
        return False

    meta = data.get("Meta", {})
    duration = float(meta.get("Duration", 0.0) or 0.0)
    fps = float(meta.get("Fps", 60.0) or 60.0)
    if duration <= 0.0 or fps <= 0.0:
        return False

    # 过渡段采样点数：按动画自身帧率采样 smoothstep，保证视觉连续；
    # 取 3 的倍数，与 .motion3.json 的 type-1 线性段「3 点/段」格式对齐
    point_count = 3 * max(1, int(round(blend * fps / 3.0)))

    modified = 0
    for curve in data.get("Curves", []):
        segments = curve.get("Segments", [])
        keyframes = _parse_segments(segments)
        if len(keyframes) < 2:
            continue  # 常量曲线，循环点本就连续
        first_val = keyframes[0][1]
        last_val = keyframes[-1][1]
        if abs(last_val - first_val) <= _LOOP_SEAMLESS_EPS:
            continue  # 首尾一致，无需过渡
        # 追加 smoothstep 过渡：u 从 1→0，值从尾帧 last_val 平滑滑到首帧
        # first_val，循环点前后连续。按文件格式每 3 个采样点组成一个 type-1
        # 线性段（[1, t, v, t, v, t, v]），段首自动衔接上一条曲线末尾的
        # (Duration, last_val)
        transition: List[float] = []
        for g in range(point_count // 3):
            transition.append(1)  # type-1 线性段起始标记
            for k in range(3):
                i = g * 3 + k + 1
                u = (point_count - i) / point_count  # 1 → 0
                w = u * u * (3.0 - 2.0 * u)          # smoothstep：两端速度连续
                t = duration + blend * i / point_count
                v = last_val + (first_val - last_val) * (1.0 - w)
                transition.extend([t, v])
        curve["Segments"] = segments + transition
        modified += 1

    if modified == 0:
        return True  # 所有曲线首尾本就一致，无需处理

    # 元数据计数随追加段同步增加（1 段 = 3 点，与文件原有计数口径一致）
    added_segments = modified * (point_count // 3)
    meta["Duration"] = duration + blend
    if "TotalSegmentCount" in meta:
        meta["TotalSegmentCount"] = int(meta["TotalSegmentCount"]) + added_segments
    if "TotalPointCount" in meta:
        meta["TotalPointCount"] = int(meta["TotalPointCount"]) + added_segments * 3

    # 首次修改前备份原文件，保证可还原
    backup = path + _SEAMLESS_BACKUP_SUFFIX
    try:
        if not os.path.exists(backup):
            import shutil
            shutil.copy2(path, backup)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
    except Exception as e:
        console.dim(f"无缝化：写回 {os.path.basename(path)} 失败（{e}）")
        return False
    return True


def _parse_segments(segments: list) -> List[Tuple[float, float]]:
    """解析 .motion3.json 曲线段为 ``[(time, value), ...]`` 列表。"""
    if len(segments) < 2:
        return [(0.0, 0.0)]

    base_value = segments[1]
    keyframes = [(0.0, base_value)]

    rest = segments[2:]
    i = 0
    while i < len(rest):
        # 跳过段分隔符（"1" = segment available hint）
        if rest[i] == 1.0:
            i += 1
            continue
        if i + 1 < len(rest):
            t, v = float(rest[i]), float(rest[i + 1])
            keyframes.append((t, v))
            i += 2
        else:
            break

    # 按时间排序 + 去重（同一时间保留最后值）
    keyframes.sort(key=lambda x: x[0])
    deduped: List[Tuple[float, float]] = []
    for kf in keyframes:
        if deduped and deduped[-1][0] == kf[0]:
            deduped[-1] = kf
        else:
            deduped.append(kf)
    return deduped


def _interpolate(keyframes: List[Tuple[float, float]], t: float) -> float:
    """线性插值获取指定时间的参数值。"""
    if not keyframes:
        return 0.0
    if t <= keyframes[0][0]:
        return keyframes[0][1]
    if t >= keyframes[-1][0]:
        return keyframes[-1][1]
    for i in range(len(keyframes) - 1):
        t0, v0 = keyframes[i]
        t1, v1 = keyframes[i + 1]
        if t0 <= t < t1:
            if t1 == t0:
                return v0
            return v0 + (v1 - v0) * (t - t0) / (t1 - t0)
    return keyframes[-1][1]


class MotionPlayer:
    """播放 .motion3.json 动作文件，逐帧输出参数值。

    :ivar tracked_ids: 该动效控制的 VTS 跟踪参数 ID 集合
    :ivar active: 是否正在播放
    """

    def __init__(self, path: str = "") -> None:
        self._frames: List[Dict[str, float]] = []
        self._frame_count = 0
        self._fps = 60.0
        self._duration = 0.0
        self._loop = True
        self._start_time: float = 0.0
        self._active = False
        self.path: str = path  # 动作文件路径（幂等重载判断用）

        # 公开属性
        self.tracked_ids: Set[str] = set()

        if path:
            self.load(path)

    def load(self, path: str) -> None:
        """加载并解析 .motion3.json 文件，预计算所有帧。"""
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)

        meta = data.get("Meta", {})
        self._duration = float(meta.get("Duration", 0.0))
        self._fps = float(meta.get("Fps", 60.0))
        self._loop = bool(meta.get("Loop", True))

        frame_count = max(1, int(self._duration * self._fps))
        self._frames = [{} for _ in range(frame_count)]
        self._frame_count = frame_count
        self._first_frame: Dict[str, float] = {}   # 首帧原始值（循环混合锚点）

        # 无缝循环判定：所有曲线首尾关键帧值一致（误差内）则无需混合。
        # 作者在 Live2D Animator 里按无缝循环导出的动画（首尾帧相同），
        # 循环点本身不会跳变，混合反而会扭曲动作路径，应原样播放。
        # 注意按「关键帧」而非「预计算帧网格」比对：帧网格采样不到动画
        # 末尾（最后一帧距 Duration 差 ~1 帧），无缝文件会被误判为不连续。
        # 该口径与 make_seamless 的无缝化判定一致（关键帧首尾值比对）。
        self._seamless = True
        for curve in data.get("Curves", []):
            param_id = curve.get("Id", "")
            if not param_id:
                continue
            segments = curve.get("Segments", [])
            keyframes = _parse_segments(segments)
            if len(keyframes) >= 2 and abs(keyframes[-1][1] - keyframes[0][1]) > _LOOP_SEAMLESS_EPS:
                self._seamless = False

            if len(keyframes) < 2:
                val = keyframes[0][1] if keyframes else 0.0
                for fi in range(frame_count):
                    self._frames[fi][param_id] = val
            else:
                for fi in range(frame_count):
                    t = fi / self._fps
                    self._frames[fi][param_id] = _interpolate(keyframes, t)

            # 如果参数有映射，也记录到 tracked_ids
            if param_id in _PARAM_MAP:
                self.tracked_ids.add(_PARAM_MAP[param_id])
            if param_id in _PARAM_EXPAND:
                self.tracked_ids.update(_PARAM_EXPAND[param_id])

        console.info(f"动效已加载：{len(self._frames)} 帧"
                     f"（{self._duration:.1f}s @ {self._fps:.0f}fps），"
                     f"映射跟踪参数 {len(self.tracked_ids)} 个")
        # 记录首帧（循环播放尾帧→首帧混合用）：
        # 动作第 0 帧往往只含部分参数（曲线延后才出现，如 ParamAngleX），
        # 若只取 frames[0]，混合时缺参参数会回退当前值 → 循环点仍跳变。
        # 改为取「每个参数最早出现的值」作为首帧。
        self._first_frame: Dict[str, float] = {}
        for _f in self._frames:
            for _pid, _v in _f.items():
                if _pid not in self._first_frame:
                    self._first_frame[_pid] = _v

    # ---- 生命周期 ----

    def start(self) -> None:
        """开始播放。"""
        self._start_time = time.time()
        self._active = True

    def stop(self) -> None:
        """停止播放。"""
        self._active = False

    @property
    def active(self) -> bool:
        return self._active

    # ---- 帧查询 ----

    def _resolve_raw_frame(self, elapsed: float) -> Dict[str, float]:
        """按播放时间解析当前帧原始值；循环模式下尾帧自动混合回首帧。

        .motion3.json 首尾帧通常不衔接，直接取模会在循环点突跳。
        在最后 _LOOP_BLEND_WINDOW 秒内，把当前值平滑混合到首帧值，
        使循环点前后连续，动作自然"回到原点"。

        用 smoothstep 缓动替代线性插值：线性混合在窗口起点（刚接上动画
        原始速度）和循环点（落到首帧后立即以新速度起步）两端都有速度突变，
        看起来像「卡一下再弹回」；smoothstep 两端导数为 0，先平滑减速到 0、
        再平滑起步，过渡更自然。
        """
        if not self._frames:
            return {}
        if self._loop and self._duration > 0:
            t = elapsed % self._duration
        else:
            t = min(elapsed, self._duration) if self._duration > 0 else 0

        idx = min(int(t * self._fps), self._frame_count - 1)
        raw = self._frames[idx]

        # 尾帧→首帧平滑混合（原生无缝循环跳过，避免扭曲动作路径）
        if (self._loop and not self._seamless
                and self._duration > 0 and self._first_frame):
            remain = self._duration - t
            if remain <= _LOOP_BLEND_WINDOW:
                u = remain / _LOOP_BLEND_WINDOW  # 1 → 0
                if u < 1.0:
                    w = u * u * (3.0 - 2.0 * u)  # smoothstep：两端速度连续
                    # 对「当前帧 ∪ 首帧」的全部参数混合，防止某参数只出现在
                    # 一侧时循环点缺参跳变
                    blended: Dict[str, float] = {}
                    for pid in set(raw) | set(self._first_frame):
                        cur = raw.get(pid, self._first_frame[pid])
                        anchor = self._first_frame.get(pid, cur)
                        blended[pid] = cur * w + anchor * (1.0 - w)
                    raw = blended
        return raw

    def get_frame(self, elapsed: Optional[float] = None) -> Dict[str, float]:
        """获取当前帧的参数值字典（已映射到 VTS 跟踪参数名）。

        :param elapsed: 播放经过的秒数，None 则使用当前实际时间
        :returns: ``{param_name: value, ...}``
        """
        if not self._frames or not self._active:
            return {}
        if elapsed is None:
            elapsed = time.time() - self._start_time

        raw = self._resolve_raw_frame(elapsed)

        # 映射参数名 + 展开
        result: Dict[str, float] = {}
        for pid, val in raw.items():
            if pid in _PARAM_MAP:
                result[_PARAM_MAP[pid]] = val
            elif pid in _PARAM_EXPAND:
                for expanded_pid in _PARAM_EXPAND[pid]:
                    result[expanded_pid] = val
            # 跳过未知自定义参数（VTS 不接受）
        return result

    def get_raw_frame(self, elapsed: Optional[float] = None) -> Dict[str, float]:
        """获取原始帧（不重命名参数名）。"""
        if not self._frames or not self._active:
            return {}
        if elapsed is None:
            elapsed = time.time() - self._start_time
        return dict(self._resolve_raw_frame(elapsed))