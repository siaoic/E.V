"""桌宠交互模块：待机循环 / 动作播放 / 参数注入 / 气泡字幕 / 点击穿透 / 鼠标交互。

每个函数接收 self（PetWidget 实例）作为首参数，方法体逐字来自
原 src/pet/widget.py 的同名方法，逻辑零改动。由 core.PetWidget 的
同名方法转发调用，保证方法归属仍在 PetWidget 类上。
"""

from __future__ import annotations

import os
import random
import sys
import time
from typing import Dict, List, Optional, Tuple

from PySide6.QtCore import Qt
from PySide6.QtGui import QCursor, QMouseEvent

import live2d.v3 as live2d

from ev.utils import console

from .bubble import (
    _MOTION_FILE_GROUP,
    _declared_idle_file,
    _motion_base_name,
)


# 循环判定阈值（与原 widget.py 顶部常量完全一致）——定义在这里是因为
# 它们仅被本模块函数使用；core.py 需要时通过 import 取用。
_LOOP_ADVANCE = 0.1
_STALL_GRACE = 0.5
_USER_GRACE = 1.0
_UNKNOWN_MOTION_TIMEOUT = 90.0
_WIN_GWL_EXSTYLE = -20
_WIN_WS_EX_TRANSPARENT = 0x00000020


def _motion_duration(self, path: str) -> float:
    """按动作文件路径查时长（秒）。大小写不敏感。"""
    if not path:
        return 0.0
    base = os.path.basename(path).lower()
    for k, v in self._motion_durations.items():
        if k.lower() == base:
            return v
    return 0.0


def _start_auto_idle(self, announce: bool = True) -> None:
    """自动循环播放待机动作。"""
    model = self.model
    if model is None:
        return
    prefer = str(getattr(self.cfg, "PET_IDLE_MOTION", "") or "").strip()
    candidates: List[Tuple[str, int, float]] = []
    # 1) 显式配置：MotionFile 组按文件名匹配
    if prefer:
        for i, mp in enumerate(self._motion_ok_files):
            if _motion_base_name(mp) == prefer:
                candidates.append(
                    (_MOTION_FILE_GROUP, i, _motion_duration(self, mp)))
                break
    # 2) 模型声明的 Idle 组
    if self._motion_groups.get("Idle"):
        f = _declared_idle_file(self._model_path, self.cfg.PROJECT_ROOT)
        candidates.append(("Idle", 0, _motion_duration(self, f)))
    # 3) 文件名含「待机」/idle/loop 智能匹配
    for i, mp in enumerate(self._motion_ok_files):
        base = _motion_base_name(mp)
        if ("待机" in base or base.lower().startswith("idle")
                or "loop" in base.lower()):
            candidates.append(
                (_MOTION_FILE_GROUP, i, _motion_duration(self, mp)))
    if not candidates:
        return
    self._idle_candidates = candidates
    self._idle_candidate_idx = 0
    for idx in range(len(candidates)):
        if _play_idle_candidate(self, idx):
            self._idle_candidate_idx = idx
            if announce:
                group, no, _ = candidates[idx]
                console.info(f"自动循环播放待机动作：{group}/{no}")
            return
    console.error("待机动作全部播放失败，模型将保持静止（可重启或更换模型）")


def _play_idle_candidate(self, idx: int) -> bool:
    """播放第 idx 个待机候选并验证真的播起来。"""
    model = self.model
    if model is None or not (0 <= idx < len(self._idle_candidates)):
        return False
    group, no, duration = self._idle_candidates[idx]
    try:
        model.StopAllMotions()
        model.StartMotion(group, no, live2d.MotionPriority.FORCE)
        model.Update()
        if model.IsMotionFinished():
            console.dim(f"待机动作未实际播放，跳过：{group}/{no}")
            return False
        self._idle_ctx = (group, no, duration, time.monotonic())
        return True
    except Exception as e:
        console.dim(f"待机动作播放失败（{group}/{no}）：{e}")
        return False


def _play_idle(self) -> None:
    """无缝重播待机动作，失败自动换候选。"""
    n = len(self._idle_candidates)
    if n == 0:
        return
    start = self._idle_candidate_idx % n
    for k in range(n):
        idx = (start + k) % n
        if _play_idle_candidate(self, idx):
            self._idle_candidate_idx = idx
            return
    self._idle_ctx = None
    console.error("待机动作全部播放失败，模型将保持静止（可重启或更换模型）")


def _restart_idle(self) -> None:
    """用户动作播完后恢复待机。"""
    self._idle_candidate_idx = 0
    _play_idle(self)


def _tick_idle_loop(self) -> None:
    """待机动作无缝循环（paintGL 每帧调用）。"""
    model = self.model
    if model is None:
        return
    # ---- 用户动作：播完 / 超时 → 恢复待机循环 ----
    if self._user_motion is not None:
        group, no, dur, started = self._user_motion
        try:
            elapsed = time.monotonic() - started
            finished = model.IsMotionFinished()
            if dur > 0:
                timed_out = elapsed >= dur + _USER_GRACE
            else:
                timed_out = elapsed >= _UNKNOWN_MOTION_TIMEOUT
            if finished or timed_out:
                if timed_out and not finished:
                    console.dim(
                        f"用户动作 {group}/{no} 播放超时，强制恢复待机循环")
                self._user_motion = None
                _restart_idle(self)
        except Exception:
            pass
        return
    # ---- 待机循环 ----
    ctx = self._idle_ctx
    if ctx is None:
        return
    group, no, duration, started = ctx
    try:
        elapsed = time.monotonic() - started
        if duration > _LOOP_ADVANCE:
            if elapsed >= duration - _LOOP_ADVANCE \
                    or elapsed >= duration + _STALL_GRACE:
                _play_idle(self)
        else:
            if elapsed >= _LOOP_ADVANCE and model.IsMotionFinished():
                _play_idle(self)
    except Exception:
        pass


def set_parameters(self, params: Dict[str, float]) -> None:
    """缓存 FaceDriver 注入的参数（由 paintGL 在 Update 后统一应用）。"""
    self._pending_params.update(params)


def play_expression(self, name: str) -> bool:
    """播放指定表情（exp3.json 的 Name/id）。"""
    model = self.model
    if model is None or not name:
        return False
    try:
        if name in model.GetExpressionIds():
            model.SetExpression(name)
            return True
    except Exception as e:
        console.dim(f"表情播放失败：{e}")
    return False


def play_motion(self, group: str, no: int) -> bool:
    """播放指定动作（Motions 组名 + 序号）。"""
    model = self.model
    if model is None or not group:
        return False
    count = self._motion_groups.get(group)
    if count is None or not (0 <= no < count):
        return False
    try:
        model.StartMotion(group, no, live2d.MotionPriority.FORCE)
        self._user_motion = (group, no, 0.0, time.monotonic())
        return True
    except Exception as e:
        console.dim(f"动作播放失败：{e}")
    return False


def play_motion_by_name(self, name: str) -> bool:
    """按文件名（去扩展名）播放 MotionFile 组的动作。"""
    model = self.model
    if model is None or not name:
        return False
    for i, mp in enumerate(self._motion_ok_files):
        base = _motion_base_name(mp)
        if base == name:
            try:
                model.StartMotion(
                    _MOTION_FILE_GROUP, i, live2d.MotionPriority.FORCE)
                dur = _motion_duration(self, mp)
                self._user_motion = (
                    _MOTION_FILE_GROUP, i, dur, time.monotonic())
                return True
            except Exception as e:
                console.dim(f"动作播放失败：{e}")
            return False
    return False


def motion_groups(self) -> Dict[str, int]:
    """动作组名 → 动作数量。"""
    return dict(self._motion_groups)


def show_text(self, text: str, speed_ms: int = 0) -> None:
    """显示一句气泡字幕（持续显示，直到 clear_text 清除）。"""
    if not text:
        return
    self._bubble.setText(text)
    self._bubble.adjustSize()
    bw, bh = self._bubble.width(), self._bubble.height()
    self._bubble.setGeometry(
        max(8, (self.width() - bw) // 2),
        max(8, self.height() - bh - 60),
        min(bw, self.width() - 16),
        bh,
    )
    self._bubble.show()


def clear_text(self) -> None:
    """立即清除气泡字幕。"""
    self._bubble.hide()


def _hit_model(self, x: float, y: float) -> bool:
    """检测 (x, y) 是否落在模型命中区域（透明像素穿透拖拽）。"""
    model = self.model
    if model is None:
        return True
    try:
        return bool(model.HitPart(x, y, False))
    except Exception:
        return True  # 检测失败则按整窗可拖


def _update_click_through(self) -> None:
    """按鼠标位置动态切换窗口点击穿透（仅 Windows）。"""
    if not self.isVisible() or self._in_system_move \
            or not sys.platform.startswith("win"):
        return
    pos = self.mapFromGlobal(QCursor.pos())
    want = self.rect().contains(pos) \
        and not _hit_model(self, pos.x(), pos.y())
    if want != self._click_through_on:
        self._click_through_on = want
        _set_click_through(self, want)


def _set_click_through(self, enable: bool) -> None:
    """设置窗口鼠标穿透（Win32 WS_EX_TRANSPARENT）。"""
    try:
        import ctypes
        hwnd = int(self.winId())
        user32 = ctypes.windll.user32
        style = user32.GetWindowLongW(hwnd, _WIN_GWL_EXSTYLE)
        if enable:
            style |= _WIN_WS_EX_TRANSPARENT
        else:
            style &= ~_WIN_WS_EX_TRANSPARENT
        user32.SetWindowLongW(hwnd, _WIN_GWL_EXSTYLE, style)
    except Exception:
        pass


def _play_tap_motion(self) -> None:
    """点击模型：随机播放一组点击动作（优先 TapBody 组）。"""
    model = self.model
    if model is None:
        return
    group = "TapBody" if self._motion_groups.get("TapBody") else None
    try:
        if group:
            count = self._motion_groups[group]
            model.StartMotion(group, random.randrange(count),
                              live2d.MotionPriority.FORCE)
            self._user_motion = (group, -1, 0.0, time.monotonic())
        else:
            model.StartRandomMotion(
                priority=live2d.MotionPriority.FORCE)
            self._user_motion = ("*random*", -1, 0.0, time.monotonic())
    except Exception as e:
        console.dim(f"点击动作播放失败：{e}")


def mousePressEvent(self, event: QMouseEvent) -> None:
    if event.button() != Qt.MouseButton.LeftButton:
        return
    pos = event.position()
    if _hit_model(self, pos.x(), pos.y()):
        _play_tap_motion(self)
        # 系统级拖动（Qt 5.15+ QWindow.startSystemMove()）
        wh = self.windowHandle()
        if wh is not None:
            self._in_system_move = True
            try:
                wh.startSystemMove()
            finally:
                self._in_system_move = False
            if self.isVisible():
                self.update()
    event.accept()


def mouseMoveEvent(self, event: QMouseEvent) -> None:
    event.accept()


def mouseReleaseEvent(self, event: QMouseEvent) -> None:
    if self.isVisible():
        self.update()
    event.accept()
