"""桌宠窗口：PySide6 QOpenGLWidget + live2d-py 渲染本地 Live2D 模型。

窗口特性（对齐 live2d-py 官方桌宠示例）：
- 透明背景、无边框、可置顶
- 按住模型区域拖拽移动窗口；点击模型随机播放 TapBody 动作
- 气泡字幕：说哪句显示哪句（复用 stream 的 sub 接口，无需改管线）
- 自动眨眼/呼吸由 FaceDriver 与基线动作接管，禁用模型内建动画

参数注入：PetFaceDriver 每帧调用 set_parameters()（VTS 参数名 → Cubism 原生名
→ SetParameterValue），与 vtuber 模式共用同一套口型/眨眼/节拍计算。
"""

import glob
import json
import os
import random
import sys
import time
from typing import Dict, List, Optional, Tuple

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QCursor, QFont, QFontDatabase, QMouseEvent, QSurfaceFormat
from PySide6.QtOpenGLWidgets import QOpenGLWidget
from PySide6.QtWidgets import QApplication, QLabel

import OpenGL.GL as GL   # 官方示例：Draw 前显式设置 GL 状态

import live2d.v3 as live2d

from src.utils import console

# 自定义字幕字体文件（相对项目根目录 assets/ 下；打包后与 exe 同目录的 assets/ 亦可）
_SUBTITLE_FONT_FILE = os.path.join("assets", "ArtierEN-2.ttf")


def _load_subtitle_font() -> QFont:
    """加载自定义字幕字体（ArtierEN-2.ttf），失败回退微软雅黑。"""
    from src.utils import config as _config

    candidates = [
        os.path.join(_config.cfg.PROJECT_ROOT, _SUBTITLE_FONT_FILE),
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "ArtierEN-2.ttf"),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "ArtierEN-2.ttf"),
    ]
    for p in candidates:
        if not os.path.isfile(p):
            continue
        fid = QFontDatabase.addApplicationFont(p)
        families = QFontDatabase.applicationFontFamilies(fid)
        if families:
            return QFont(families[0], 13)
        break
    return QFont("Microsoft YaHei", 13)

# VTS 跟踪参数名 → Cubism 原生参数名（face_driver 计算的动效/眨眼参数是 VTS 名，
# motion_player._PARAM_MAP 的反向映射；已是 "Param" 开头的原生名直接透传）
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

# 渲染帧率（Qt timer 驱动，30fps 与 FaceDriver 注入同频即可）
_FPS = 60

# 模型目录自带的动作文件（model3.json 未声明的也收录）：
# 扫描 <模型目录>/motion 与 /motions 子目录下的 *.motion3.json，
# 运行时 LoadExtraMotion 注册为 MotionFile 组——「有什么用什么」。
_MOTION_FILE_GROUP = "MotionFile"
_MOTION_DIRS = ("motion", "motions")

# 待机无缝循环提前量（秒）：在动作结束前提前重播，此刻模型姿态还在末帧附近，
# 新动作从首帧开始——Loop 动作首尾帧接近，衔接几乎无感；等真正播完再播
# 会出现「回静止 pose → 再起跳」的两次跳变。
_LOOP_ADVANCE = 0.1

# 预重播失败/卡帧后的强制重播宽限（秒）：elapsed 已超过 duration + _STALL_GRACE
# 仍没触发预重播（说明上一轮重播失败或 paintGL 卡帧）→ 强制重播，保证循环不中断。
_STALL_GRACE = 0.5

# 用户动作播放结束判定：
#   - 时长已知：播完 + _USER_GRACE 宽限即视为结束（IsMotionFinished 提前返回/播放误差兜底）；
#   - 时长未知：用 _UNKNOWN_MOTION_TIMEOUT 长上限兜底——防点击随机到 Loop 长动作时
#     IsMotionFinished 永不结束、待机循环被永久卡死。
_USER_GRACE = 1.0
_UNKNOWN_MOTION_TIMEOUT = 90.0

# 模型空白区鼠标穿透（Win32 WS_EX_TRANSPARENT）：
# 轮询鼠标位置切换穿透的周期（毫秒）。穿透开启后本窗口收不到鼠标事件，
# 只能靠系统级 GetCursorPos 判断位置，故用定时器而不是鼠标事件驱动。
_CLICK_THROUGH_POLL_MS = 50
# Win32 窗口扩展样式索引（GetWindowLongW/SetWindowLongW 的 GWL_EXSTYLE）
_WIN_GWL_EXSTYLE = -20
# Win32 鼠标穿透扩展样式位（WS_EX_TRANSPARENT）
_WIN_WS_EX_TRANSPARENT = 0x00000020


def _scan_motion_files(model3_path: str) -> List[str]:
    """模型目录 motion/motions 子目录下的 .motion3.json 文件（排序，绝对路径）。"""
    base = os.path.dirname(os.path.abspath(model3_path))
    files = []
    for d in _MOTION_DIRS:
        files.extend(glob.glob(os.path.join(base, d, "*.motion3.json")))
    return sorted(set(files))


def _motion_base_name(path: str) -> str:
    """动作文件显示/匹配名：去掉 .motion3.json / .motion3 / .json 后缀。

    控制中心「动作绑定区域」与桌宠播放端共用此命名（文件名去扩展名），
    避免 splitext 只去掉最后一层 .json 留下「Hiyori_m01.motion3」的脏名。
    """
    base = os.path.basename(path)
    for suf in (".motion3.json", ".motion3", ".json"):
        if base.lower().endswith(suf):
            return base[: -len(suf)]
    return base


class PetWidget(QOpenGLWidget):
    """桌宠渲染窗口。"""

    def __init__(self, cfg) -> None:
        super().__init__()
        self.cfg = cfg
        self.model = None
        self._driver = None   # PetFaceDriver（口型/眨眼注入源），attach_driver 挂载
        # FaceDriver 注入的参数缓存：set_parameters 只写入此 dict，paintGL 在
        # model.Update() 之后统一应用——保证注入值覆盖待机动作曲线，避免
        # Update() 与 SetParameterValue 调用顺序不固定导致的眨眼抽搐
        self._pending_params: Dict[str, float] = {}
        self._param_ranges: Dict[str, Tuple[float, float]] = {}
        self._motion_groups: Dict[str, int] = {}   # 动作组名 -> 动作数量
        self._motion_files: List[str] = []         # 模型目录自带动作文件（MotionFile 组）
        self._motion_ok_files: List[str] = []      # 其中实际注册成功的文件（序号按此）
        self._motion_durations: Dict[str, float] = {}  # 动作文件名 -> 时长（秒，无缝循环用）
        self._idle_ctx: Optional[Tuple[str, int, float, float]] = None   # 自动待机 (组, 序号, 时长, 开始时刻)
        self._idle_candidates: List[Tuple[str, int, float]] = []  # 待机候选（按优先级，失败自动换下一个）
        self._idle_candidate_idx: int = 0                          # 当前生效的待机候选序号
        self._user_motion: Optional[Tuple[str, int, float, float]] = None  # 用户动作 (组, 序号, 时长, 开始时刻)
        # 渲染健康状态：paintGL 异常/渲染停止（GL 上下文丢失等）时自动重载模型自恢复
        self._last_paint = time.time()             # 最近一次成功绘制时间
        self._paint_errors = 0                     # 连续渲染异常计数
        self._err_log_at = 0.0                     # 异常日志限频时间戳
        self._reload_pending = False               # 待重载标记（GL context 活动时执行）
        self._last_reload_at = 0.0                 # 重载冷却（防止反复重建模型）

        # 窗口：透明 + 无边框 +（可选）置顶
        self.setWindowTitle("E.V")
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint)
        if cfg.PET_ALWAYS_ON_TOP:
            self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        # 官方透明窗口示例（issue #98）：QOpenGLWidget 默认会填充背景，
        # 透明窗口必须关闭自动填背景；alpha 走 Qt 合成层，GL 侧开 4x MSAA。
        self.setAutoFillBackground(False)
        _fmt = QSurfaceFormat()
        _fmt.setAlphaBufferSize(0)
        _fmt.setSamples(4)
        self.setFormat(_fmt)
        # 窗口尺寸：.env 显式配置 PET_WINDOW_SIZE 优先，否则自适应主屏
        size = self._resolve_window_size(cfg.PET_WINDOW_SIZE)
        if size is None:
            size = self._adaptive_size()
        self.resize(*size)
        # 窗口初始位置：主屏可用区域正中央（避开任务栏）；此后可随意拖动
        self._center_on_screen()

        # 气泡字幕（QLabel 覆盖在 GL 画面上，透明圆角半透明底）
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

        # 模型空白区点击穿透：鼠标位于透明区域时点击落到下层窗口。
        # 定时按鼠标位置切换 Win32 WS_EX_TRANSPARENT（见 _update_click_through）
        self._click_through_on = False   # 当前是否已开启穿透（避免重复 SetWindowLong）
        self._in_system_move = False     # 系统拖动中禁止切换样式，避免干扰拖动
        self._click_timer = QTimer(self)
        self._click_timer.timeout.connect(self._update_click_through)
        self._click_timer.start(_CLICK_THROUGH_POLL_MS)

        self._load_motion_groups()

    @staticmethod
    def _resolve_window_size(size_spec: str) -> Optional[Tuple[int, int]]:
        """解析 "宽x高" 窗口尺寸；未配置/格式错误返回 None。"""
        try:
            w, h = str(size_spec or "").lower().split("x")
            return int(w), int(h)
        except Exception:
            return None

    @staticmethod
    def _adaptive_size() -> Tuple[int, int]:
        """桌宠窗口自适应主屏：高度取可用高度 70%，宽度按默认 500x700 比例。

        小屏有下限兜底（320x480），避免窗口过小看不清模型。
        """
        scr = QApplication.primaryScreen()
        if scr is None:
            return 500, 700
        h = int(scr.availableGeometry().height() * 0.70)
        w = int(h * 500 / 700)
        return max(320, w), max(480, h)

    def _center_on_screen(self) -> None:
        """把窗口移到主屏可用区域正中央（避开任务栏）。"""
        scr = QApplication.primaryScreen()
        if scr is None:
            return
        area = scr.availableGeometry()
        geo = self.frameGeometry()
        geo.moveCenter(area.center())
        self.move(geo.topLeft())

    # ---------- 模型加载（Qt 渲染线程，首次绘制时调用） ----------

    def _model_path(self) -> str:
        path = str(self.cfg.PET_MODEL_PATH).strip()
        if not path:
            return ""
        if not os.path.isabs(path):
            path = os.path.join(self.cfg.PROJECT_ROOT, path)
        return path

    def _load_motion_groups(self) -> None:
        """解析 model3.json 声明的动作组 + 扫描模型目录 motion/motions 子目录动作文件。"""
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
            # 模型目录自带动作文件（model3.json 未声明的也收录，如 MO/motion/）
            self._motion_files = _scan_motion_files(path)
            if self._motion_files:
                self._motion_groups[_MOTION_FILE_GROUP] = len(self._motion_files)
        except Exception as e:
            console.dim(f"桌宠动作组解析失败（点击动作不可用）：{e}")

    def initializeGL(self) -> None:
        """OpenGL 上下文就绪后初始化 live2d 并加载模型（仅在 GL context 活动时调用）。"""
        try:
            # 官方透明窗口示例：上下文创建后开启混合（Qt 合成层按 alpha 混合）
            GL.glEnable(GL.GL_BLEND)
            GL.glBlendFunc(GL.GL_SRC_ALPHA, GL.GL_ONE_MINUS_SRC_ALPHA)
            live2d.glInit()
            self._load_model()
            self.startTimer(int(1000 / _FPS))
        except Exception as e:
            self.model = None
            console.error(f"桌宠模型加载失败（以空白窗口继续，不影响对话）：{e}")

    def switch_model(self, path: str) -> bool:
        """热切换桌宠模型：更新配置 → 标记重载（paintGL 在 GL context 活动时重建）。

        必须在 GUI 线程调用（asyncio 泵桥与 Qt 主线程同线程，可直接调用）。
        模型真正加载/重载后统一触发 on_model_loaded 回调（emotion_actor 借此
        全量重扫参数/表情/动作）。
        """
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
        self._last_reload_at = 0.0   # 跳过渲染健康检测的 5s 冷却，允许立即重载
        self.update()
        return True

    def apply_config(self, cfg) -> None:
        """控制中心「更新配置」热推：应用窗口置顶/尺寸/待机动作（立即生效）。

        必须在 GUI 线程调用（asyncio 泵桥与 Qt 主线程同线程，可直接调用）。
        只处理桌宠相关配置；模型路径变化由 switch_model / _watch_pet_model_change
        另行处理。待机动作变化时重选并重播待机候选。
        """
        self.cfg = cfg
        # 窗口置顶：动态切换 WindowStaysOnTopHint 需要 hide + setWindowFlag + show
        top = bool(getattr(cfg, "PET_ALWAYS_ON_TOP", True))
        if top != bool(self.windowFlags() & Qt.WindowType.WindowStaysOnTopHint):
            self.hide()
            self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, top)
            self.show()
        # 窗口尺寸：.env 显式配置 PET_WINDOW_SIZE 才调整；留空 = 自适应屏幕
        size = self._resolve_window_size(getattr(cfg, "PET_WINDOW_SIZE", ""))
        if size is not None:
            self.resize(*size)
        # 待机动作：重选候选（模型已加载且当前是自动待机时生效）
        if self.model is not None:
            self._start_auto_idle(announce=True)

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
        # live2d 的 LoadExtraMotion 加载失败时返回的仍是「将占用的槽位号」（与下一个
        # 成功文件相同），无法靠返回值区分成败——损坏文件（如 MO 的 基础动作.motion3.json，
        # 结构合法但 Cubism 解析失败）会虚占槽位，让后续动作序号在 Python 侧错位，
        # 播放时误报 `motion(MotionFile_N) has no file attached`。对策（两层）：
        #   1) 槽位冲突剔除：同一槽位号被多个文件返回时，后返回者才是真正注册成功的
        #      （失败不占槽位，槽位会被下一个成功文件占用）；
        #   2) 尾部探针：连续的最后一个文件失败时没有后续文件暴露冲突，用
        #      StartMotion + Update + IsMotionFinished 探针验证槽位是否真的可播。
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
        # 无论模型是否声明 Idle 组都启动待机循环：live2d-py 的 C++ 不会自动循环
        # Idle，只播放一次（或由探针遗留动作播放一次）后停在静止 pose「定住不动」
        self._start_auto_idle()
        # 模型加载/重载完成后统一通知外部（emotion_actor 借此全量重扫参数）；
        # 初次加载与热切换（switch_model）都走这里，避免回调漏触发
        cb = getattr(self, "on_model_loaded", None)
        if callable(cb):
            try:
                cb(self)
            except Exception as e:
                console.dim(f"模型加载回调失败：{e}")
        self._paint_errors = 0
        self._last_paint = time.time()

    def _declared_idle_file(self) -> str:
        """model3.json 声明的 Idle 组第一个动作文件绝对路径；无则空串。"""
        try:
            with open(self._model_path(), "r", encoding="utf-8") as f:
                data = json.load(f)
            entries = data.get("FileReferences", {}).get("Motions", {}).get("Idle") or []
            if entries and entries[0].get("File"):
                return os.path.normpath(os.path.join(
                    os.path.dirname(os.path.abspath(self._model_path())),
                    str(entries[0]["File"]),
                ))
        except Exception:
            pass
        return ""

    def _motion_duration(self, path: str) -> float:
        """按动作文件路径查时长（秒）。

        键大小写不敏感：model3.json 声明的 File 可能与磁盘实际文件名大小写
        不一致（如肥牛声明 motions/hiyori_m01.motion3.json、磁盘实际是
        Hiyori_m01.motion3.json），直接 dict.get 会漏匹配导致时长=0。
        """
        if not path:
            return 0.0
        base = os.path.basename(path).lower()
        for k, v in self._motion_durations.items():
            if k.lower() == base:
                return v
        return 0.0

    def _start_auto_idle(self, announce: bool = True) -> None:
        """自动循环播放待机动作（无 Idle 组 / 有 Idle 组都启用）。

        待机候选选择优先级（全部命中项都收进候选列表，便于失败换下一个）：
          1) .env 的 PET_IDLE_MOTION（控制中心「动作绑定区域」配置，文件名去扩展名）
             在 MotionFile 组（模型目录 motion/motions 子目录）中按名匹配；
          2) model3.json 声明的 Idle 组第一个动作（模型作者指定的待机）；
          3) 未配置时按文件名含「待机」/idle/loop 智能匹配。

        只负责选候选 + 首播；无缝循环由 _tick_idle_loop 每帧检测提前重播。
        某个候选播放失败（序号错位/文件损坏）时自动换下一个，绝不停死。
        """
        model = self.model
        if model is None:
            return
        prefer = str(getattr(self.cfg, "PET_IDLE_MOTION", "") or "").strip()
        candidates: List[Tuple[str, int, float]] = []   # (组, 序号, 时长秒)
        # 1) 显式配置：MotionFile 组按文件名匹配（序号取实际注册成功列表）
        if prefer:
            for i, mp in enumerate(self._motion_ok_files):
                if _motion_base_name(mp) == prefer:
                    candidates.append(
                        (_MOTION_FILE_GROUP, i, self._motion_duration(mp)))
                    break
        # 2) 模型声明的 Idle 组
        if self._motion_groups.get("Idle"):
            f = self._declared_idle_file()
            candidates.append(("Idle", 0, self._motion_duration(f)))
        # 3) 文件名含「待机」/idle/loop 智能匹配（全部命中作候选）
        for i, mp in enumerate(self._motion_ok_files):
            base = _motion_base_name(mp)
            if ("待机" in base or base.lower().startswith("idle")
                    or "loop" in base.lower()):
                candidates.append(
                    (_MOTION_FILE_GROUP, i, self._motion_duration(mp)))
        if not candidates:
            return
        self._idle_candidates = candidates
        self._idle_candidate_idx = 0
        for idx in range(len(candidates)):
            if self._play_idle_candidate(idx):
                self._idle_candidate_idx = idx
                if announce:
                    group, no, _ = candidates[idx]
                    console.info(f"自动循环播放待机动作：{group}/{no}")
                return
        console.error("待机动作全部播放失败，模型将保持静止（可重启或更换模型）")

    def _play_idle_candidate(self, idx: int) -> bool:
        """播放第 idx 个待机候选并验证真的播起来；成功记录 _idle_ctx，失败返回 False。

        必须先停再播：Cubism 的 StartMotion 在同/低优先级下会被拒绝
        （C++ 打印 "motion priority is too low"）——待机循环重播时上一次
        动作还没播完，直接 StartMotion 不会生效，动作播完就会停在
        静止 pose（模型「站在原地不动」）。
        用 FORCE 播放：Cubism 对 FORCE 无条件放行——_currentPriority 不会
        因 StopAllMotions 复位，若用 NORMAL，会先后被「_load_model 探针
        遗留的 FORCE」和「用户点击动作的 FORCE」顶掉（current>=2 即拒绝），
        表现为待机/恢复待机全部失败、模型静止。
        验证方式借鉴 _load_model 的尾部探针：StartMotion 后 Update() 推进
        一帧，若 IsMotionFinished 立即为 True → 动作没有真正播放
        （序号错位/文件损坏），判定失败由上层换候选，避免「播一次后定住」。
        """
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
        """无缝重播待机动作（预重播 / 超时兜底调用），失败自动换候选。

        当前候选失败 → 依次尝试其余候选（绕回），全部失败才清上下文停循环
        （此时确实无动作可播，打诊断日志，不再静默定住）。
        """
        n = len(self._idle_candidates)
        if n == 0:
            return
        start = self._idle_candidate_idx % n
        for k in range(n):
            idx = (start + k) % n
            if self._play_idle_candidate(idx):
                self._idle_candidate_idx = idx
                return
        # 全部候选失败：清掉上下文防止每帧重试刷屏，并给出诊断
        self._idle_ctx = None
        console.error("待机动作全部播放失败，模型将保持静止（可重启或更换模型）")

    def _restart_idle(self) -> None:
        """用户动作播完后恢复待机：从候选 0（最高优先级）开始，失败自动换候选。"""
        self._idle_candidate_idx = 0
        self._play_idle()

    def _tick_idle_loop(self) -> None:
        """待机动作无缝循环（paintGL 每帧调用）。

        状态互斥：
          - _user_motion 非 None：用户动作播放中——待机让位；动作播完
            （IsMotionFinished）或超时（时长已知超 1s / 未知时长超长上限，
            防点击随机到 Loop 长动作永不结束卡死待机）→ 恢复待机循环。
          - _idle_ctx 非 None：待机播放中——
            时长已知：结束前 _LOOP_ADVANCE 秒预重播（此刻姿态还在末帧附近，
              新动作从首帧开始；Loop 动作首尾帧接近，衔接几乎无感）；超过
              duration + _STALL_GRACE 仍未预重播（卡帧/失败）→ 强制重播。
            时长未知（读不到 Duration）：IsMotionFinished 播完检测兜底。
        """
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
                    self._restart_idle()
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
                # 时长已知：以时长预测为主——实测 IsMotionFinished 在动作真正
                # 播完前就返回 True（循环间隔缩到 ~0.1s，频繁重播会抽搐），
                # 只在结束前 _LOOP_ADVANCE 秒预重播实现无缝衔接。
                # _STALL_GRACE 兜底：上一轮预重播失败/卡帧时强制重播，
                # 保证循环不中断（不会「播完一次就定住」）。
                if elapsed >= duration - _LOOP_ADVANCE \
                        or elapsed >= duration + _STALL_GRACE:
                    self._play_idle()
            else:
                # 时长未知（读不到 Duration）：播完检测兜底
                if elapsed >= _LOOP_ADVANCE and model.IsMotionFinished():
                    self._play_idle()
        except Exception:
            pass

    # ---------- 参数注入（PetFaceDriver 30fps 调用，主线程安全） ----------

    def attach_driver(self, driver) -> None:
        """挂载 FaceDriver（口型/眨眼参数注入源），模型重载时通知其刷新。"""
        self._driver = driver

    def set_parameters(self, params: Dict[str, float]) -> None:
        """缓存 FaceDriver 注入的参数（由 paintGL 在 Update 后统一应用）。

        不直接调 SetParameterValue：model.Update() 会应用待机动作曲线到
        参数上，若 Update 与 SetParameterValue 调用顺序不固定（FaceDriver
        30fps 与 paintGL 60fps 交替），眨眼值与动作曲线值每帧互踩→抽搐。
        改为缓存到 _pending_params，paintGL 在 Update 之后统一 SetParameterValue，
        保证注入值始终是最后生效的。
        """
        self._pending_params.update(params)

    def _apply_pending_params(self) -> None:
        """paintGL 在 model.Update() 之后调用：把缓存的注入参数应用到模型。

        Update() 已推进待机动作并应用曲线值，此刻 SetParameterValue 覆盖
        眨眼/口型参数为 FaceDriver 的值，确保注入值始终最终生效。
        """
        model = self.model
        if model is None or not self._pending_params:
            return
        ranges = self._param_ranges
        for vid, value in self._pending_params.items():
            pid = _FACE_TO_CUBISM.get(vid, vid)
            if pid not in ranges:
                continue  # 模型没有该参数（如 VTS 自定义口型参数）
            pmin, pmax = ranges[pid]
            try:
                model.SetParameterValue(pid, float(max(pmin, min(pmax, value))))
            except Exception:
                pass  # 单参数失败不影响整帧（模型参数在动画中会临时变化）

    # ---------- 表情 / 动作播放（embedding 情绪控制用） ----------

    def play_expression(self, name: str) -> bool:
        """播放指定表情（exp3.json 的 Name/id）。无该表情返回 False。"""
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
        """播放指定动作（Motions 组名 + 序号）。参数不合法返回 False。"""
        model = self.model
        if model is None or not group:
            return False
        count = self._motion_groups.get(group)
        if count is None or not (0 <= no < count):
            return False
        try:
            model.StartMotion(group, no, live2d.MotionPriority.FORCE)
            # 用户动作播放中：待机无缝循环让位，等它播完（IsMotionFinished
            # 或超时兜底）再恢复；时长未知按 0 处理（走长上限兜底）
            self._user_motion = (group, no, 0.0, time.monotonic())
            return True
        except Exception as e:
            console.dim(f"动作播放失败：{e}")
        return False

    def play_motion_by_name(self, name: str) -> bool:
        """按文件名（去扩展名）播放 MotionFile 组的动作；找不到返回 False。

        控制中心「动作绑定区域」绑定的是动作文件名，序号在加载失败剔除后
        可能错位（LoadExtraMotion 失败虚占槽位），按文件名匹配最稳。
        """
        model = self.model
        if model is None or not name:
            return False
        for i, mp in enumerate(self._motion_ok_files):
            base = _motion_base_name(mp)
            if base == name:
                try:
                    model.StartMotion(
                        _MOTION_FILE_GROUP, i, live2d.MotionPriority.FORCE)
                    # 用户动作播放中：待机让位；时长已知时播完+宽限即恢复
                    dur = self._motion_duration(mp)
                    self._user_motion = (
                        _MOTION_FILE_GROUP, i, dur, time.monotonic())
                    return True
                except Exception as e:
                    console.dim(f"动作播放失败：{e}")
                return False
        return False

    def motion_groups(self) -> Dict[str, int]:
        """动作组名 → 动作数量（embedding 控制与命令解析用）。"""
        return dict(self._motion_groups)

    # ---------- 气泡字幕（复用 stream 的 sub 接口） ----------

    def show_text(self, text: str, speed_ms: int = 0) -> None:
        """显示一句气泡字幕（持续显示，直到 clear_text 清除）。

        不再按字数自动隐藏：TTS 逐字推送时句间合成间隙可达数秒，短定时器
        会在回复中途把气泡藏起又显示，造成闪烁。字幕生命周期统一由
        clear_text 控制（回复结束 / 打断时调用）。
        """
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
        """立即清除气泡字幕（回复结束 / 打断时）。"""
        self._bubble.hide()

    # ---------- Qt 渲染回调 ----------

    def timerEvent(self, event) -> None:
        # 渲染健康检测：若 GL 上下文丢失（休眠/唤醒/GPU 重置）导致渲染停止，
        # 标记重载挂起（由 paintGL 在 context 活动时执行），并限频防止反复重建。
        # 阈值 2.5s：瞬时 GPU 繁忙（TTS 合成/记忆嵌入/模型重载同抢显卡）可能
        # 让画帧饿死 >1s，过紧会把偶发卡顿误判成 GL 丢失，反而触发 2~3s 的全量
        # 重载（26 条 motion 刷屏 + GPU 更忙）；2.5s 仍能对真实上下文丢失快速自愈。
        if self.model is not None and time.time() - self._last_paint > 2.5:
            if not self._reload_pending and time.time() - self._last_reload_at > 5.0:
                console.error("渲染停止（GL 上下文可能丢失），正在重新加载模型…")
                self._reload_pending = True
        if self.isVisible() and self.model is not None:
            self.update()

    def paintGL(self) -> None:
        # 重载挂起 → 在 GL context 活动时执行模型重建（自恢复）
        if self._reload_pending:
            self._reload_pending = False
            self._last_reload_at = time.time()
            try:
                self._load_model()
            except Exception as e:
                console.error(f"桌宠模型重载失败：{e}")
        if self.model is None:
            return
        try:
            live2d.clearBuffer()
            self.model.Update()
            self._tick_idle_loop()   # 待机无缝循环：结束前预重播 / 用户动作播完恢复
            # 在 Update() 之后应用 FaceDriver 注入的参数（眨眼/口型），
            # 覆盖待机动作曲线值——消除两者调用顺序不固定导致的眨眼抽搐
            self._apply_pending_params()
            # 官方 main_glfw.py 绘制协议：Draw 前必须显式设置 GL 状态。
            # Qt 每帧会重置 GL 状态，直接 Draw() 时混合/深度等状态丢失，
            # 预乘 alpha 纹理可能以错误方式合成 → 白底/花屏。
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
            # 白屏自愈：拖动等场景下 GL 上下文损坏时 Draw 可能静默失败
            # （不抛异常，_paint_errors 不会累积）→ 画面变白卡住。
            # 显式查 GL 错误状态，有错则按渲染异常处理，连续多次自动重载。
            if not self._check_gl_error():
                raise RuntimeError("GL state error")
            self._paint_errors = 0
            self._last_paint = time.time()
        except Exception as e:
            # 渲染异常：日志限频；连续多次 → 标记重载（可能是纹理/GL 状态损坏）
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
        """Draw 后检查 GL 错误状态；无错误返回 True。

        live2d-py 的 Draw 在 GL 上下文损坏（拖动/休眠/GPU 重置）时不会抛
        Python 异常，而是留下 GL 错误码，画面表现为白屏。这里把错误状态
        转成 False，由 paintGL 计入渲染异常 → 连续多次自动重载模型自愈。
        """
        try:
            if GL.glGetError() == GL.GL_NO_ERROR:
                return True
            # 清空积压错误（可能有多个），避免后续帧重复命中
            while GL.glGetError() != GL.GL_NO_ERROR:
                pass
            return False
        except Exception:
            return True  # 检测本身失败不误判

    def resizeGL(self, w: int, h: int) -> None:
        if self.model is not None:
            self.model.Resize(w, h)

    # ---------- 鼠标交互：拖拽 + 点击动作 ----------

    def _hit_model(self, x: float, y: float) -> bool:
        """检测 (x, y) 是否落在模型命中区域（透明像素穿透拖拽）。

        官方示例用 model.HitPart()（模型空间命中检测，纯 CPU/数学计算，
        完全不经过 GL）。替代原 glReadPixels + makeCurrent/doneCurrent——
        后者在按下瞬间切换 GL 上下文并做像素回读（GPU 同步），正是
        拖动时 GL 状态被破坏、画面变白的诱因之一。
        入参为窗口本地坐标（与 Resize 相同的逻辑坐标空间，y 向下）。
        """
        model = self.model
        if model is None:
            return True
        try:
            return bool(model.HitPart(x, y, False))
        except Exception:
            return True  # 检测失败则按整窗可拖

    def _update_click_through(self) -> None:
        """按鼠标位置动态切换窗口点击穿透（仅 Windows）。

        鼠标位于模型空白区（未命中模型）→ 开启穿透，点击落到下层窗口；
        位于模型命中区 → 关闭穿透，可点击/拖动模型。
        用轮询而非鼠标事件驱动：穿透开启后本窗口收不到鼠标事件，
        只能靠系统级 GetCursorPos 判断位置（QTimer 不受穿透影响）。
        """
        if not self.isVisible() or self._in_system_move \
                or not sys.platform.startswith("win"):
            return
        pos = self.mapFromGlobal(QCursor.pos())
        want = self.rect().contains(pos) \
            and not self._hit_model(pos.x(), pos.y())
        if want != self._click_through_on:
            self._click_through_on = want
            self._set_click_through(want)

    def _set_click_through(self, enable: bool) -> None:
        """设置窗口鼠标穿透（Win32 WS_EX_TRANSPARENT）；非 Windows / 失败静默。

        WS_EX_TRANSPARENT 使整窗对命中测试透明——点击直接落到下层窗口。
        ctypes 懒加载（对齐项目约定，模块顶层不 import ctypes）。
        """
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
            pass  # 穿透切换失败不影响模型渲染/交互

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
                # 点击动作播放完再恢复待机循环；时长未知 → 走长上限兜底
                # （防随机到 Loop 长动作时 IsMotionFinished 永不结束卡死待机）
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
        if self._hit_model(pos.x(), pos.y()):
            self._play_tap_motion()
            # 系统级拖动（Qt 5.15+ QWindow.startSystemMove()）：让操作系统接管
            # 窗口移动，替代手动 move()。高频 mouseMove + move() 会触发透明
            # 窗口整窗重绘风暴，拖动中 GL 上下文频繁切换导致卡顿/白屏；
            # startSystemMove 期间窗口移动由系统处理、Qt 事件照常派发
            # （渲染不中断），从根本上消除卡顿与 GL 状态损坏。
            wh = self.windowHandle()
            if wh is not None:
                self._in_system_move = True
                try:
                    wh.startSystemMove()
                finally:
                    self._in_system_move = False
                # 拖动结束（阻塞返回）强制重绘一次，确保画面立即恢复
                if self.isVisible():
                    self.update()
        event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        # 窗口拖动已交由 startSystemMove 系统级处理，这里无需手动 move()
        event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        # 系统拖动结束时若还有残留的「未重绘帧」，强制重绘一次恢复画面
        if self.isVisible():
            self.update()
        event.accept()


class BubbleSub:
    """把 PetWidget 的字幕接进 stream 的 sub 接口（push text/clear）。

    用法：`sub = BubbleSub(pet_widget)` 传给 main/stream，
    stream.py / proactive.py 无需感知渲染目标。
    """

    def __init__(self, widget: PetWidget) -> None:
        self.widget = widget

    def push(self, kind: str, text: str = "", speed_ms: int = 0) -> None:
        try:
            if kind == "text":
                self.widget.show_text(str(text), speed_ms)
            elif kind == "clear":
                self.widget.clear_text()
            # "user"（用户发言）在桌宠模式不显示气泡
        except Exception:
            pass

    def stop(self) -> None:
        """对齐 SubtitleServer 的生命周期接口（main.py finally 统一调用）。"""
        try:
            self.widget.clear_text()
        except Exception:
            pass
