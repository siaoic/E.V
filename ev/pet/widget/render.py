"""桌宠渲染模块：GL 初始化 / 绘制 / 模型加载 / 参数应用 等长方法。

每个函数接收 self（PetWidget 实例）作为首参数，方法体逐字来自
原 src/pet/widget.py 的同名方法，逻辑零改动。由 core.PetWidget 的
同名方法转发调用，保证方法归属仍在 PetWidget 类上（import 0 改动）。
"""

from __future__ import annotations

import json
import os
import time
from typing import Dict, Tuple

import OpenGL.GL as GL   # 官方示例：Draw 前显式设置 GL 状态
import live2d.v3 as live2d

from ev.utils import console

from .bubble import (
    _MOTION_FILE_GROUP,
    _scan_motion_files,
)


def initializeGL(self) -> None:
    """OpenGL 上下文就绪后初始化 live2d 并加载模型（仅在 GL context 活动时调用）。"""
    try:
        # 官方透明窗口示例：上下文创建后开启混合（Qt 合成层按 alpha 混合）
        GL.glEnable(GL.GL_BLEND)
        GL.glBlendFunc(GL.GL_SRC_ALPHA, GL.GL_ONE_MINUS_SRC_ALPHA)
        live2d.glInit()
        _load_model(self)
        # FPS 常量在 core.py 顶部定义并通过 self 上的 timer 驱动，
        # 这里直接按 60fps 启动定时器（与 core 顶部 _FPS=60 对齐）。
        from .core import _FPS
        self.startTimer(int(1000 / _FPS))
    except Exception as e:
        self.model = None
        console.error(f"桌宠模型加载失败（以空白窗口继续，不影响对话）：{e}")


def _load_model(self) -> None:
    """加载/重载 Live2D 模型 + 缓存参数范围 + 注册自带动作。

    必须在 GL context 活动时调用（initializeGL / paintGL 内）。
    重载前先释放旧模型渲染器，避免旧 context 的 GL 资源泄漏。
    """
    path = self._model_path()
    if not path or not os.path.isfile(path):
        raise RuntimeError(
            f"桌宠模型不存在：{path or '<未配置 PET_MODEL_PATH>'}\n"
            f"  请在 .env 设置 PET_MODEL_PATH 指向 .model3.json")
    if self.model is not None:
        try:
            self.model.DestroyRenderer()
        except Exception:
            pass
        self.model = None
    self._pending_params.clear()   # 清空旧参数缓存，避免新模型应用残留值
    self.model = live2d.LAppModel()
    self.model.LoadModelJson(path)
    self.model.Resize(self.width(), self.height())
    # 自动眨眼/呼吸由 FaceDriver 口型循环与基线动作接管，避免冲突
    self.model.SetAutoBlinkEnable(False)
    self.model.SetAutoBreathEnable(False)
    # 缓存参数范围（SetParameterValue 需 clamp 到 [min, max]）
    self._param_ranges = {}
    for i in range(self.model.GetParameterCount()):
        p = self.model.GetParameter(i)
        self._param_ranges[p.id] = (float(p.min), float(p.max))
    # 注册模型目录自带动作（LoadExtraMotion 加入 MotionFile 组，「有什么用什么」）。
    self._motion_ok_files = []
    slot_owner: Dict[int, str] = {}   # 槽位号 -> 该槽位实际归属的动作文件
    for mp in self._motion_files:
        try:
            r = self.model.LoadExtraMotion(_MOTION_FILE_GROUP, mp)
        except Exception as e:
            console.dim(f"动作 {os.path.basename(mp)} 加载失败：{e}")
            continue
        if not (isinstance(r, int) and r >= 0):
            console.dim(f"动作 {os.path.basename(mp)} 加载失败（文件损坏或不受支持）")
            continue
        prev = slot_owner.get(r)
        if prev is not None:
            # 同一槽位号被再次返回 → 前一个文件实际加载失败，槽位被本次占用
            console.dim(f"动作 {os.path.basename(prev)} 加载失败（文件损坏或不受支持）")
        slot_owner[r] = mp
    # 尾部探针：最后一个文件连续失败时虚占的槽位没有冲突可暴露，逐个验证可播性
    while slot_owner:
        idx = max(slot_owner)
        try:
            self.model.StopAllMotions()
            self.model.StartMotion(_MOTION_FILE_GROUP, idx, live2d.MotionPriority.FORCE)
            self.model.Update()
            if not self.model.IsMotionFinished():
                break   # 该槽位真的能播，其下槽位均合法
        except Exception:
            break
        bad = slot_owner.pop(idx)
        console.dim(f"动作 {os.path.basename(bad)} 加载失败（文件损坏或不受支持）")
    self._motion_ok_files = [mp for _, mp in sorted(slot_owner.items())]
    ok_count = len(self._motion_ok_files)
    if ok_count:
        self._motion_groups[_MOTION_FILE_GROUP] = ok_count
    # 探针会遗留一个 FORCE 动作在播（验证槽位可播用），清掉避免它播放一次后冻结
    try:
        self.model.StopAllMotions()
    except Exception:
        pass
    # 缓存动作时长（Meta.Duration）：无缝循环在结束前 _LOOP_ADVANCE 秒预重播
    self._motion_durations = {}
    for mp in self._motion_ok_files:
        try:
            with open(mp, "r", encoding="utf-8") as _f:
                dur = float(json.load(_f).get("Meta", {}).get("Duration", 0) or 0)
        except Exception:
            dur = 0.0
        self._motion_durations[os.path.basename(mp)] = dur
    # 重置自动待机/用户动作状态（重载后重新从待机循环开始）
    self._idle_ctx = None
    self._idle_candidates = []
    self._idle_candidate_idx = 0
    self._user_motion = None
    console.ok(f"桌宠模型已加载：{os.path.basename(path)}"
               f"（参数 {len(self._param_ranges)} 个，动作组 "
               f"{'、'.join(f'{g}×{n}' for g, n in self._motion_groups.items())}）")
    # 无论模型是否声明 Idle 组都启动待机循环
    from .interact import _start_auto_idle
    _start_auto_idle(self)
    # 模型加载/重载完成后统一通知外部（emotion_actor 借此全量重扫参数）
    cb = getattr(self, "on_model_loaded", None)
    if callable(cb):
        try:
            cb(self)
        except Exception as e:
            console.dim(f"模型加载回调失败：{e}")
    self._paint_errors = 0
    self._last_paint = time.time()


def timerEvent(self, event) -> None:
    """Qt 定时器回调：渲染健康检测 + 触发 update()。"""
    if self.model is not None and time.time() - self._last_paint > 2.5:
        if not self._reload_pending and time.time() - self._last_reload_at > 5.0:
            console.error("渲染停止（GL 上下文可能丢失），正在重新加载模型…")
            self._reload_pending = True
    if self.isVisible() and self.model is not None:
        self.update()


def paintGL(self) -> None:
    """每帧 GL 绘制。"""
    if self._reload_pending:
        self._reload_pending = False
        self._last_reload_at = time.time()
        try:
            _load_model(self)
        except Exception as e:
            console.error(f"桌宠模型重载失败：{e}")
    if self.model is None:
        return
    try:
        live2d.clearBuffer()
        self.model.Update()
        # 待机无缝循环：结束前预重播 / 用户动作播完恢复
        from .interact import _tick_idle_loop
        _tick_idle_loop(self)
        # 在 Update() 之后应用 FaceDriver 注入的参数（眨眼/口型），
        # 覆盖待机动作曲线值——消除两者调用顺序不固定导致的眨眼抽搐
        _apply_pending_params(self)
        # 官方 main_glfw.py 绘制协议：Draw 前必须显式设置 GL 状态。
        GL.glPushAttrib(GL.GL_CURRENT_BIT | GL.GL_ENABLE_BIT
                        | GL.GL_POLYGON_BIT | GL.GL_COLOR_BUFFER_BIT)
        GL.glUseProgram(0)
        GL.glDisable(GL.GL_DEPTH_TEST)
        GL.glDisable(GL.GL_CULL_FACE)
        GL.glEnable(GL.GL_BLEND)
        GL.glBlendFuncSeparate(GL.GL_ONE, GL.GL_ONE_MINUS_SRC_ALPHA,
                               GL.GL_ONE, GL.GL_ONE_MINUS_SRC_ALPHA)
        self.model.Draw()
        GL.glPopAttrib()
        if not _check_gl_error():
            raise RuntimeError("GL state error")
        self._paint_errors = 0
        self._last_paint = time.time()
    except Exception as e:
        self._paint_errors += 1
        now = time.time()
        if now - self._err_log_at > 5.0 or self._paint_errors <= 2:
            self._err_log_at = now
            console.error(f"渲染异常（第 {self._paint_errors} 次）："
                          f"{type(e).__name__}: {e}")
        if self._paint_errors >= 5 and now - self._last_reload_at > 5.0:
            console.error("渲染连续异常，尝试重新加载模型…")
            self._reload_pending = True


def _check_gl_error(self) -> bool:
    """Draw 后检查 GL 错误状态；无错误返回 True。"""
    try:
        if GL.glGetError() == GL.GL_NO_ERROR:
            return True
        while GL.glGetError() != GL.GL_NO_ERROR:
            pass
        return False
    except Exception:
        return True  # 检测本身失败不误判


def resizeGL(self, w: int, h: int) -> None:
    if self.model is not None:
        self.model.Resize(w, h)


def _apply_pending_params(self) -> None:
    """paintGL 在 model.Update() 之后调用：把缓存的注入参数应用到模型。"""
    from .core import _FACE_TO_CUBISM
    model = self.model
    if model is None or not self._pending_params:
        return
    ranges = self._param_ranges
    for vid, value in self._pending_params.items():
        pid = _FACE_TO_CUBISM.get(vid, vid)
        if pid not in ranges:
            continue
        pmin, pmax = ranges[pid]
        try:
            model.SetParameterValue(pid, float(max(pmin, min(pmax, value))))
        except Exception:
            pass  # 单参数失败不影响整帧
