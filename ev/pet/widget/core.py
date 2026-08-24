"""PetWidget 类骨架（定义类、属性、__init__、短小静态方法；长方法体委派到其他子模块）。

拆分策略（调用方 0 改动 / 逻辑零改动）：
- 类体中所有方法仍定义在 PetWidget 上（import 后 isinstance / 方法绑定无差异）；
- 短小骨架方法（__init__、静态尺寸计算、模型路径、动作组扫描、
  attach_driver、switch_model、apply_config）逐字保留在本文件；
- 长方法（GL 绘制、模型加载、待机循环、动作播放、鼠标交互、
  气泡显示）仅保留签名，方法体转调 render / interact 子模块的同名函数。
"""

from __future__ import annotations

import json
import os
import time
from typing import Dict, List, Optional, Tuple

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QMouseEvent, QSurfaceFormat
from PySide6.QtOpenGLWidgets import QOpenGLWidget
from PySide6.QtWidgets import QApplication, QLabel

from ev.utils import console

from . import render as _render_mod
from . import interact as _interact_mod
from .bubble import _load_subtitle_font, _scan_motion_files


# VTS 跟踪参数名 → Cubism 原生参数名
_FACE_TO_CUBISM: Dict[str, str] = {
    "FaceAngleX": "ParamAngleX",
    "FaceAngleY": "ParamAngleY",
    "FaceAngleZ": "ParamAngleZ",
    "BrowLeftY": "ParamBrowLY",
    "BrowRightY": "ParamBrowRY",
    "BrowLeftX": "ParamBrowLForm",
    "BrowRightX": "ParamBrowRForm",
    "EyeOpenLeft": "ParamEyeLOpen",
    "EyeOpenRight": "ParamEyeROpen",
    "EyeLeftX": "ParamEyeBallX",
    "EyeRightX": "ParamEyeBallX",
    "EyeLeftY": "ParamEyeBallY",
    "EyeRightY": "ParamEyeBallY",
    "MouthSmile": "ParamMouthForm",
    "MouthOpen": "ParamMouthOpenY",
}

# 渲染帧率（Qt timer 驱动）
_FPS = 60

# 点击穿透轮询周期（毫秒）
_CLICK_THROUGH_POLL_MS = 50


class PetWidget(QOpenGLWidget):
    """桌宠渲染窗口。"""

    def __init__(self, cfg) -> None:
        super().__init__()
        self.cfg = cfg
        self.model = None
        self._driver = None
        self._pending_params: Dict[str, float] = {}
        self._param_ranges: Dict[str, Tuple[float, float]] = {}
        self._motion_groups: Dict[str, int] = {}
        self._motion_files: List[str] = []
        self._motion_ok_files: List[str] = []
        self._motion_durations: Dict[str, float] = {}
        self._idle_ctx: Optional[Tuple[str, int, float, float]] = None
        self._idle_candidates: List[Tuple[str, int, float]] = []
        self._idle_candidate_idx: int = 0
        self._user_motion: Optional[Tuple[str, int, float, float]] = None
        self._last_paint = time.time()
        self._paint_errors = 0
        self._err_log_at = 0.0
        self._reload_pending = False
        self._last_reload_at = 0.0

        # 窗口：透明 + 无边框 +（可选）置顶
        self.setWindowTitle("E.V")
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint)
        if cfg.PET_ALWAYS_ON_TOP:
            self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAutoFillBackground(False)
        _fmt = QSurfaceFormat()
        _fmt.setAlphaBufferSize(0)
        _fmt.setSamples(4)
        self.setFormat(_fmt)
        size = self._resolve_window_size(cfg.PET_WINDOW_SIZE)
        if size is None:
            size = self._adaptive_size()
        self.resize(*size)
        self._center_on_screen()

        # 气泡字幕（QLabel 覆盖在 GL 画面上）
        self._bubble = QLabel(self)
        self._bubble.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._bubble.setWordWrap(True)
        self._bubble.setFont(_load_subtitle_font())
        self._bubble.setStyleSheet(
            "background-color: rgba(30, 30, 46, 200);"
            "color: #ffffff;"
            "border-radius: 12px;"
            "padding: 8px 16px;"
        )
        self._bubble.hide()

        # 模型空白区点击穿透
        self._click_through_on = False
        self._in_system_move = False
        self._click_timer = QTimer(self)
        self._click_timer.timeout.connect(self._update_click_through)
        self._click_timer.start(_CLICK_THROUGH_POLL_MS)

        self._load_motion_groups()

    @staticmethod
    def _resolve_window_size(size_spec: str) -> Optional[Tuple[int, int]]:
        try:
            w, h = str(size_spec or "").lower().split("x")
            return int(w), int(h)
        except Exception:
            return None

    @staticmethod
    def _adaptive_size() -> Tuple[int, int]:
        scr = QApplication.primaryScreen()
        if scr is None:
            return 500, 700
        h = int(scr.availableGeometry().height() * 0.70)
        w = int(h * 500 / 700)
        return max(320, w), max(480, h)

    def _center_on_screen(self) -> None:
        scr = QApplication.primaryScreen()
        if scr is None:
            return
        area = scr.availableGeometry()
        geo = self.frameGeometry()
        geo.moveCenter(area.center())
        self.move(geo.topLeft())

    # ---------- 模型加载骨架 ----------

    def _model_path(self) -> str:
        path = str(self.cfg.PET_MODEL_PATH).strip()
        if not path:
            return ""
        if not os.path.isabs(path):
            path = os.path.join(self.cfg.PROJECT_ROOT, path)
        return path

    def _load_motion_groups(self) -> None:
        try:
            path = self._model_path()
            if not path or not os.path.isfile(path):
                return
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            motions = data.get("FileReferences", {}).get("Motions", {}) or {}
            self._motion_groups = {
                name: len(entries or []) for name, entries in motions.items()
            }
            self._motion_files = _scan_motion_files(path)
            if self._motion_files:
                from .bubble import _MOTION_FILE_GROUP
                self._motion_groups[_MOTION_FILE_GROUP] = len(self._motion_files)
        except Exception as e:
            console.dim(f"桌宠动作组解析失败（点击动作不可用）：{e}")

    def switch_model(self, path: str) -> bool:
        rel = (path or "").strip()
        if not rel:
            return False
        abs_path = rel if os.path.isabs(rel) else os.path.join(
            self.cfg.PROJECT_ROOT, rel)
        if not os.path.isfile(abs_path):
            return False
        self.cfg.PET_MODEL_PATH = rel
        self._load_motion_groups()
        self._reload_pending = True
        self._last_reload_at = 0.0
        self.update()
        return True

    def apply_config(self, cfg) -> None:
        self.cfg = cfg
        top = bool(getattr(cfg, "PET_ALWAYS_ON_TOP", True))
        if top != bool(self.windowFlags() & Qt.WindowType.WindowStaysOnTopHint):
            self.hide()
            self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, top)
            self.show()
        size = self._resolve_window_size(getattr(cfg, "PET_WINDOW_SIZE", ""))
        if size is not None:
            self.resize(*size)
        if self.model is not None:
            _interact_mod._start_auto_idle(self, announce=True)

    # ---------- 参数注入 / 挂载 ----------

    def attach_driver(self, driver) -> None:
        self._driver = driver

    def set_parameters(self, params: Dict[str, float]) -> None:
        _interact_mod.set_parameters(self, params)

    # ---------- 表情 / 动作播放 ----------

    def play_expression(self, name: str) -> bool:
        return _interact_mod.play_expression(self, name)

    def play_motion(self, group: str, no: int) -> bool:
        return _interact_mod.play_motion(self, group, no)

    def play_motion_by_name(self, name: str) -> bool:
        return _interact_mod.play_motion_by_name(self, name)

    def motion_groups(self) -> Dict[str, int]:
        return _interact_mod.motion_groups(self)

    # ---------- 气泡字幕 ----------

    def show_text(self, text: str, speed_ms: int = 0) -> None:
        _interact_mod.show_text(self, text, speed_ms)

    def clear_text(self) -> None:
        _interact_mod.clear_text(self)

    # ---------- Qt 渲染回调（转调 render 模块） ----------

    def initializeGL(self) -> None:
        _render_mod.initializeGL(self)

    def timerEvent(self, event) -> None:
        _render_mod.timerEvent(self, event)

    def paintGL(self) -> None:
        _render_mod.paintGL(self)

    def resizeGL(self, w: int, h: int) -> None:
        _render_mod.resizeGL(self, w, h)

    # ---------- 鼠标交互（转调 interact 模块） ----------

    def _hit_model(self, x: float, y: float) -> bool:
        return _interact_mod._hit_model(self, x, y)

    def _update_click_through(self) -> None:
        _interact_mod._update_click_through(self)

    def _set_click_through(self, enable: bool) -> None:
        _interact_mod._set_click_through(self, enable)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        _interact_mod.mousePressEvent(self, event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        _interact_mod.mouseMoveEvent(self, event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        _interact_mod.mouseReleaseEvent(self, event)
