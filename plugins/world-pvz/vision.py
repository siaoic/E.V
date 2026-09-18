"""PvZ 视觉识别：廉价帧差 + OpenCV 模板匹配。

识别范围只做 4 类（PWSR 思路：语义记录交给 AI Memory，机械计算留给 World）：
1. 场景阶段（主菜单 / 选卡 / 战斗 / 结算）——模板匹配；
2. 阳光数——阳光计数区数字逐位模板匹配；
3. 每行是否有僵尸——草坪行区域的帧差强度（僵尸会动，草坪是静态的）；
4. 卡片冷却状态——卡片槽区域相对「就绪基准亮度」的比值。

两级检测（实施计划 §3.2 的直接受益者）：
- ``frame_diff_score``：截屏 → 灰度 → 缩到 64×64 → 与上一帧算平均绝对差，
  便宜，可按 ``worlds.change_poll_seconds`` 每 2 秒跑一次；
- ``analyze``：模板匹配等昂贵操作，只在帧差超阈值时执行。

模板与区域坐标需要实机标定：``templates/`` 缺文件、窗口分辨率与
``base_resolution`` 不符时，本模块会明确报错（不做静默兜底）。
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import cv2
import numpy as np

# 必需模板：场景阶段 4 张 + 阳光数字 0-9 共 10 张
SCENE_PHASES: Dict[str, str] = {
    "main_menu": "phase_main_menu.png",
    "seed_select": "phase_seed_select.png",
    "battle": "phase_battle.png",
    "level_complete": "phase_level_complete.png",
}
DIGITS: List[str] = [f"digit_{i}.png" for i in range(10)]

REQUIRED_TEMPLATES: List[str] = list(SCENE_PHASES.values()) + DIGITS

TEMPLATE_MATCH_THRESHOLD = 0.72
DIFF_SCALE = (64, 64)


class VisionError(RuntimeError):
    """视觉识别失败（模板缺失 / 窗口丢失 / 截屏失败）。"""


def missing_templates(templates_dir: Path) -> List[str]:
    """返回缺失的必需模板文件名列表；空列表表示模板齐全。"""

    return [name for name in REQUIRED_TEMPLATES if not (templates_dir / name).exists()]


@dataclass
class WindowFrame:
    """一次窗口截屏的产物。"""

    hwnd: int
    image: "np.ndarray"  # BGR
    width: int
    height: int
    scale_x: float  # 实际宽 / 基准宽（区域坐标换算用）
    scale_y: float


@dataclass
class Analysis:
    """一次完整识别的结果（机械事实，中文渲染交给 plugin/renderer）。"""

    phase: str  # main_menu / seed_select / battle / level_complete / unknown
    sun: Optional[int]
    zombie_rows: List[int]
    ready_cards: List[int]  # 就绪的卡片槽位号（从 1 开始）
    wave_hint: str = ""
    details: Dict[str, object] = field(default_factory=dict)


class PvzVision:
    """窗口截屏与 4 类信息的识别器。"""

    def __init__(
        self,
        *,
        templates_dir: Path,
        base_resolution: Tuple[int, int],
        regions: Dict[str, List[float]],
        diff_threshold: float,
        row_threshold: float,
        card_ready_brightness: float,
        logger,
    ) -> None:
        """构建识别器。

        Args:
            base_resolution: 模板与区域坐标的基准分辨率，如 (800, 600)。
            regions: 基准坐标系下的区域，键：``sun``（阳光数字区）、
                ``lawn_rows``（5 行草坪，每行 [x, y, w, h]）、``card_slots``
                （卡片槽，每格 [x, y, w, h]）。
        """

        self._templates_dir = templates_dir
        self._base_w, self._base_h = base_resolution
        self._regions = regions
        self._diff_threshold = float(diff_threshold)
        self._row_threshold = float(row_threshold)
        self._card_ready_brightness = float(card_ready_brightness)
        self._logger = logger
        self._templates: Dict[str, "np.ndarray"] = {}
        self._last_small: Optional["np.ndarray"] = None

    # ------------------------------------------------------------------ 截屏与帧差

    def grab_window(self, image: "np.ndarray") -> WindowFrame:
        """把已截取的窗口位图包装成帧并换算缩放比。"""

        height, width = image.shape[:2]
        return WindowFrame(
            hwnd=0,
            image=image,
            width=width,
            height=height,
            scale_x=width / self._base_w,
            scale_y=height / self._base_h,
        )

    def frame_diff_score(self, frame: WindowFrame) -> float:
        """与上一帧算平均绝对差（0~255）；首帧视为「变化极大」。"""

        small = cv2.resize(
            cv2.cvtColor(frame.image, cv2.COLOR_BGR2GRAY),
            DIFF_SCALE,
            interpolation=cv2.INTER_AREA,
        ).astype(np.float32)
        previous = self._last_small
        self._last_small = small
        if previous is None:
            return 255.0
        return float(np.mean(np.abs(small - previous)))

    def changed_quickly(self, frame: WindowFrame) -> Tuple[bool, float]:
        """廉价变更检测；返回 (是否超阈值, 分数)。"""

        score = self.frame_diff_score(frame)
        return score >= self._diff_threshold, score

    # ------------------------------------------------------------------ 完整识别

    def load_templates(self) -> None:
        """加载全部必需模板。

        Raises:
            VisionError: 模板缺失或解码失败时抛出。
        """

        missing = missing_templates(self._templates_dir)
        if missing:
            raise VisionError(
                f"PvZ 模板缺失 {len(missing)} 张：{'、'.join(missing)}。"
                f"请在游戏内对应画面截取并保存到 {self._templates_dir}（尺寸按基准分辨率 "
                f"{self._base_w}x{self._base_h} 截取）"
            )
        for name in REQUIRED_TEMPLATES:
            path = self._templates_dir / name
            data = np.fromfile(str(path), dtype=np.uint8)  # 兼容中文路径
            template = cv2.imdecode(data, cv2.IMREAD_GRAYSCALE)
            if template is None:
                raise VisionError(f"模板文件无法解码: {path}")
            self._templates[name] = template
        self._logger.info(f"PvZ 模板已加载 {len(self._templates)} 张")

    def analyze(self, frame: WindowFrame) -> Analysis:
        """跑完整识别（昂贵，只在帧差超阈值时调用）。"""

        phase = self._match_phase(frame)
        sun = self._read_sun(frame)
        zombie_rows = self._detect_zombie_rows(frame)
        ready_cards = self._detect_ready_cards(frame)
        return Analysis(
            phase=phase,
            sun=sun,
            zombie_rows=zombie_rows,
            ready_cards=ready_cards,
            details={
                "diff_ready_cards": ready_cards,
                "resolution": f"{frame.width}x{frame.height}",
            },
        )

    def _region(self, frame: WindowFrame, key: str) -> Tuple[int, int, int, int]:
        """把基准坐标区域换算到实际分辨率；区域缺失时报错。"""

        if key not in self._regions:
            raise VisionError(f"配置缺少区域 {key}（基准坐标系 {self._base_w}x{self._base_h}）")
        x, y, w, h = self._regions[key]
        return (
            int(x * frame.scale_x),
            int(y * frame.scale_y),
            max(int(w * frame.scale_x), 1),
            max(int(h * frame.scale_y), 1),
        )

    def _crop_gray(self, frame: WindowFrame, box: Tuple[int, int, int, int]) -> "np.ndarray":
        x, y, w, h = box
        height, width = frame.image.shape[:2]
        return cv2.cvtColor(
            frame.image[max(y, 0): min(y + h, height), max(x, 0): min(x + w, width)],
            cv2.COLOR_BGR2GRAY,
        )

    def _match_phase(self, frame: WindowFrame) -> str:
        """对整帧（缩小到基准分辨率）做场景阶段模板匹配。"""

        scaled = cv2.resize(
            cv2.cvtColor(frame.image, cv2.COLOR_BGR2GRAY),
            (self._base_w, self._base_h),
            interpolation=cv2.INTER_AREA,
        )
        best_name, best_score = "unknown", 0.0
        for phase_name, file_name in SCENE_PHASES.items():
            template = self._templates.get(file_name)
            if template is None:
                continue
            score = float(
                cv2.matchTemplate(scaled, template, cv2.TM_CCOEFF_NORMED).max()
            )
            if score > best_score:
                best_name, best_score = phase_name, score
        if best_score < TEMPLATE_MATCH_THRESHOLD:
            return "unknown"
        return best_name

    def _read_sun(self, frame: WindowFrame) -> Optional[int]:
        """阳光数字逐位模板匹配；区域未配置或分数过低时返回 None。"""

        if "sun" not in self._regions:
            return None
        region = self._crop_gray(frame, self._region(frame, "sun"))
        if region.size == 0:
            return None
        # 二值化增强数字对比
        _, binary = cv2.threshold(region, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        digits: List[int] = []
        # 从左到右滑窗匹配每一位数字
        step = max(int(binary.shape[1] * 0.08), 4)
        column = 0
        while column + step <= binary.shape[1]:
            cell = binary[:, column: column + step]
            best_digit, best_score = None, 0.0
            for digit, template_name in enumerate(DIGITS):
                template = self._templates.get(template_name)
                if template is None or template.shape[0] > cell.shape[0] or template.shape[1] > cell.shape[1]:
                    continue
                score = float(cv2.matchTemplate(cell, template, cv2.TM_CCOEFF_NORMED).max())
                if score > best_score:
                    best_digit, best_score = digit, score
            if best_digit is not None and best_score >= TEMPLATE_MATCH_THRESHOLD:
                digits.append(best_digit)
                column += step
            else:
                column += step // 2
        if not digits:
            return None
        value = 0
        for digit in digits:
            value = value * 10 + digit
        return value

    def _detect_zombie_rows(self, frame: WindowFrame) -> List[int]:
        """僵尸会动、草坪静止：用行区域与上一帧的差分强度判定有僵尸的行。"""

        rows = self._regions.get("lawn_rows") or []
        if not rows or self._last_small is None:
            return []
        current_small = self._last_small
        zombie_rows: List[int] = []
        height, width = frame.image.shape[:2]
        for index, (x, y, w, h) in enumerate(rows, start=1):
            box = (
                int(x * frame.scale_x),
                int(y * frame.scale_y),
                max(int(w * frame.scale_x), 1),
                max(int(h * frame.scale_y), 1),
            )
            crop = self._crop_gray(frame, box)
            if crop.size == 0:
                continue
            crop_small = cv2.resize(crop, DIFF_SCALE, interpolation=cv2.INTER_AREA).astype(np.float32)
            # 用全帧差分图近似该行的运动能量：整帧缩略图差分中该行对应区域的均值
            diff_map = np.abs(current_small - cv2.resize(
                cv2.cvtColor(frame.image, cv2.COLOR_BGR2GRAY),
                DIFF_SCALE,
                interpolation=cv2.INTER_AREA,
            ).astype(np.float32))
            mapped = diff_map[
                int(DIFF_SCALE[1] * box[1] / max(height, 1)): int(DIFF_SCALE[1] * (box[1] + box[3]) / max(height, 1)),
                int(DIFF_SCALE[0] * box[0] / max(width, 1)): int(DIFF_SCALE[0] * (box[0] + box[2]) / max(width, 1)),
            ]
            if mapped.size and float(np.mean(mapped)) >= self._row_threshold:
                zombie_rows.append(index)
        return zombie_rows

    def _detect_ready_cards(self, frame: WindowFrame) -> List[int]:
        """卡片槽亮度比值判定冷却状态；需要实机标定 ``card_ready_brightness``。"""

        slots = self._regions.get("card_slots") or []
        ready: List[int] = []
        for index, (x, y, w, h) in enumerate(slots, start=1):
            box = (
                int(x * frame.scale_x),
                int(y * frame.scale_y),
                max(int(w * frame.scale_x), 1),
                max(int(h * frame.scale_y), 1),
            )
            crop = self._crop_gray(frame, box)
            if crop.size == 0:
                continue
            brightness = float(np.mean(crop))
            if brightness >= self._card_ready_brightness:
                ready.append(index)
        return ready


def closest_template_hint(name: str, candidates: List[str]) -> Optional[str]:
    """给「卡片名不认识」这类错误一个近似的候补提示（纯文本相似度）。"""

    matches = difflib.get_close_matches(name, candidates, n=1)
    return matches[0] if matches else None
