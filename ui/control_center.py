"""AI 桌宠控制中心（协调者）：加载 control_center.ui，组合各页面与业务处理器。

用法：python -m ui.control_center

界面（布局/样式）严格参考 live-2d(2)/test222.ui，定义在 control_center.ui，
可用 Qt Designer 直接打开编辑（保持 objectName 不变即可无缝加载）。

本文件只保留协调职责：vendor/six 兼容引导、窗口装载、信号接线、页面切换与
主入口。各页逻辑与业务处理按职责拆分到子包：
  - pages/：启动页 / LLM 配置 / 设置 / 表情动作 / 记忆 / 插件 / 关于
  - handlers/：主进程管理 / 配置读写 / 外部服务 / 插件卡片 / 表情绑定
  - widgets/、dialogs/、filters/、utils/：可复用组件、弹窗、过滤器、工具
"""

import os
import sys

# 桌宠依赖（PySide6 等）装在项目内 vendor_pet/，避免污染系统环境。
# 打包后（sys.frozen）依赖已内嵌，无需也不能再用 __file__ 找 vendor_pet
#（__file__ 在打包后指向临时解压目录）。
if not getattr(sys, "frozen", False):
    _vendor = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "vendor_pet",
    )
    if os.path.isdir(_vendor) and _vendor not in sys.path:
        sys.path.insert(0, _vendor)

# PySide6 6.11 的 shibokensupport 钩子会在每个模块导入后调用 inspect.getsource
# 检查是否使用 PySide6（PYSIDE-2029）。six 的动态伪模块 six.moves 没有真实源码，
# Python 3.12 在 repr 它时会访问 loader._path，而 six 的 _SixMetaPathImporter
# 缺少该属性导致 AttributeError（dateutil 等库导入 six.moves 时触发）。
# 提前补上 _path 属性可绕开此兼容性问题。
import six
if not hasattr(six._SixMetaPathImporter, "_path"):
    six._SixMetaPathImporter._path = []

from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QFont, QIcon
from PySide6.QtUiTools import QUiLoader
from PySide6.QtWidgets import (
    QAbstractScrollArea, QApplication, QButtonGroup, QComboBox,
)

from ev.utils import config
from ui.filters.close_confirm import _CloseConfirmFilter
from ui.filters.combo_wheel import _ComboWheelGuard
from ui.filters.window_drag import _WindowDragFilter
from ui.handlers.config_handler import ConfigHandler
from ui.handlers.face_handler import FaceHandler
from ui.handlers.plugin_handler import PluginHandler
from ui.handlers.process_handler import ProcessHandler
from ui.handlers.service_handler import ServiceHandler
from ui.pages.about_page import AboutPage
from ui.pages.face_page import FacePage
from ui.pages.launch_page import LaunchPage
from ui.pages.llm_page import LLMPage
from ui.pages.memory_page import MemoryPage
from ui.pages.plugins_page import PluginsPage
from ui.pages.settings_page import SettingsPage
from ui.utils.constants import ICON_FILE, UI_FILE, _TOOLTIP_QSS
from ui.widgets.audio_drop_filter import _AudioDropFilter


class ControlCenter(LaunchPage, LLMPage, SettingsPage, FacePage, MemoryPage,
                    PluginsPage, AboutPage, ProcessHandler, ConfigHandler,
                    ServiceHandler, PluginHandler, FaceHandler):
    """组合 QUiLoader 加载的 control_center.ui；控件通过属性代理访问。

    各 mixin 提供页面初始化与业务处理方法，本类只做窗口装载与信号接线。
    """

    def __init__(self) -> None:
        self.ui = QUiLoader().load(UI_FILE)
        self.cfg = config.cfg
        self.proc: "QProcess | None" = None
        # 窗口关闭后置 True：阻止 QProcess 信号回调访问已销毁的 UI 对象
        self._closing = False
        # 日志框用等宽字体：━ 边框与空格等宽，console.header 的标题才能真正居中
        # （微软雅黑非等宽，━ 比空格宽，居中偏移）。左右分栏两个面板统一设置
        for box in (self.ui.log_chat, self.ui.log_tool):
            box.setFont(QFont("Consolas", 9))
        # 全局 tooltip 样式（QToolTip 是独立顶层窗口，QSS 需挂在 QApplication 上）
        app = QApplication.instance()
        if app is not None:
            app.setStyleSheet(_TOOLTIP_QSS)
        # 无边框圆角窗口（test222 风格）：去掉系统直角边框 + 透明背景，
        # 圆角渐变背景与描边由 QSS 绘制，四角才会真正显示为圆角。
        self.ui.setWindowFlag(Qt.FramelessWindowHint, True)
        self.ui.setAttribute(Qt.WA_TranslucentBackground, True)
        # 按住窗口空白区域拖动（qApp 级过滤器：页面/侧边栏/面板空白均可）
        self._drag = _WindowDragFilter(self.ui, QApplication.instance())
        # 关闭确认弹窗（Alt+F4 / 任务栏关闭时先确认，防止误关）
        self.ui.installEventFilter(_CloseConfirmFilter(self.ui))
        # 窗口销毁时清理 QProcess：主程序进程不随之终止，仅断开信号，
        # 防止 readyRead/finished 回调访问已删除的 UI（shiboken RuntimeError）
        self.ui.destroyed.connect(self._cleanup_on_close)
        self._hide_scrollbars()
        self._init_signals()
        self._init_state()
        # TTS 参考音频输入框：均支持拖拽音频文件。
        # 主参考为单条（拖入替换），辅助参考为多条（拖入以 | 连接追加）
        self.ed_tts_audio.setAcceptDrops(True)
        self._tts_audio_drop = _AudioDropFilter(self.ed_tts_audio, append=False)
        self.ed_tts_audio.installEventFilter(self._tts_audio_drop)
        self.ed_tts_audios.setAcceptDrops(True)
        self._tts_audios_drop = _AudioDropFilter(self.ed_tts_audios, append=True)
        self.ed_tts_audios.installEventFilter(self._tts_audios_drop)
        # 表情/动作库自动刷新（vtuber 模式轮询运行时 VTS 扫描缓存：
        # 运行时扫描/模型切换后，页面绑定库自动重建）
        self._face_lib_timer = QTimer(self.ui)
        self._face_lib_timer.setInterval(2000)
        self._face_lib_timer.timeout.connect(self._poll_face_lib)
        self._update_face_lib_timer()

    def __getattr__(self, name):
        # UI 控件代理：self.rb_vts → self.ui.rb_vts
        return getattr(self.ui, name)

    def show(self) -> None:
        self._adapt_to_screen()
        self.ui.show()

    def _adapt_to_screen(self) -> None:
        """窗口初始尺寸自适应屏幕分辨率 + 屏幕居中。

        小屏不超出可用区域，大屏保持设计尺寸；随后把窗口移到主屏
        可用区域正中央（避开任务栏）。
        """
        scr = QApplication.primaryScreen().availableGeometry()
        if scr is None:
            return
        w = min(940, int(scr.width() * 0.92))
        h = min(680, int(scr.height() * 0.9))
        self.ui.resize(w, h)
        self.ui.move(scr.x() + (scr.width() - w) // 2,
                     scr.y() + (scr.height() - h) // 2)

    def _hide_scrollbars(self) -> None:
        """隐藏界面所有滚动条（滚动功能保留：滚轮/键盘仍可滚动）。

        遍历所有后代 QAbstractScrollArea（表格/滚动区/文本区，含
        QTableWidget、QScrollArea、QPlainTextEdit 等）设为
        ScrollBarAlwaysOff——视觉清爽，长内容靠滚轮/拖动滚动
        （QScrollArea 隐藏滚动条后滚轮滚动依旧生效）。
        弹窗正文（MemoryDetailDialog）的滚动条由自身 QSS 隐藏。
        """
        for w in self.ui.findChildren(QAbstractScrollArea):
            try:
                w.setVerticalScrollBarPolicy(
                    Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
                w.setHorizontalScrollBarPolicy(
                    Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            except RuntimeError:
                pass  # 控件已被销毁（切换页面时 Qt 可能延迟清理）

    # ---------- 初始化 ----------

    def _init_signals(self) -> None:
        # 导航互斥组（QUiLoader 不支持 .ui 内 buttonGroup，改为代码建立）
        self.nav_group = QButtonGroup(self.ui)
        self.nav_group.setExclusive(True)
        for btn in (self.nav_launch, self.nav_llm, self.nav_memory,
                    self.nav_settings, self.nav_face, self.nav_tools,
                    self.nav_about):
            self.nav_group.addButton(btn)
        self.nav_launch.clicked.connect(lambda: self.stack.setCurrentIndex(0))
        self.nav_llm.clicked.connect(lambda: self.stack.setCurrentIndex(1))
        self.nav_face.clicked.connect(lambda: self.stack.setCurrentIndex(2))
        self.nav_tools.clicked.connect(lambda: self.stack.setCurrentIndex(3))
        self.nav_settings.clicked.connect(lambda: self.stack.setCurrentIndex(4))
        self.nav_memory.clicked.connect(lambda: self.stack.setCurrentIndex(5))
        self.nav_about.clicked.connect(lambda: self.stack.setCurrentIndex(6))
        # 启动/清空日志仅启动页显示（其他页面保留关闭按钮）
        self.stack.currentChanged.connect(self._on_page_changed)
        self.btn_clear_log.clicked.connect(self._clear_logs)
        self.btn_close.clicked.connect(self.ui.close)
        self.radio_pet.toggled.connect(self._on_mode_changed)
        self.btn_toggle.clicked.connect(self._toggle)
        self.btn_send.clicked.connect(self._send_text)
        self.input_edit.returnPressed.connect(self._send_text)
        # 底部全长「更新配置」按钮（对齐 test222 saveConfigButton）：保存全部配置
        self.btn_save_config.clicked.connect(self._save_config)
        # 下拉框滚轮守卫：悬停时滚动只滚列表，不误改选中值
        self._combo_wheel_guard = _ComboWheelGuard(self.ui)
        for c in self.ui.findChildren(QComboBox):
            c.installEventFilter(self._combo_wheel_guard)
        # 启动页：模型下拉切换即生效（写 .env + 桌宠模式运行中热切换 + 刷新表情动作库）
        self.combo_models.currentIndexChanged.connect(self._on_model_selected)
        # 插件：刷新状态（先重读 .env，再重建卡片）
        self.btn_refresh_tools.clicked.connect(self._refresh_tools)

    def _on_page_changed(self, idx: int) -> None:
        """启动/清空日志按钮只在启动页显示（其余页面保留关闭按钮）。"""
        launch = idx == 0
        self.btn_toggle.setVisible(launch)
        self.btn_clear_log.setVisible(launch)
        # 进入表情与动作页：刷新 vtuber 绑定库（运行时扫描缓存可能已更新）
        if idx == self._FACE_PAGE_INDEX:
            self._refresh_face_lib()

    def _init_state(self) -> None:
        # 运行模式
        if self.cfg.RUN_MODE == "pet":
            self.radio_pet.setChecked(True)
        else:
            self.radio_vts.setChecked(True)
        # 桌宠模型列表
        self._scan_models()
        # 各页面初始化（LLM / 设置 / 表情动作 / 插件 / 记忆 依次回填与构建）
        self._init_llm_page()
        self._init_settings_page()
        self._init_face_page()
        self._apply_face_mode_state()
        self._init_tools_page()
        self._init_memory_page()


def main() -> None:
    # 任务栏图标（PyInstaller -w 打包的关键修复）：Windows 7+ 按进程的
    # AppUserModelID 分组显示任务栏图标。若未显式设置，-w 打包后的 exe 在
    # 任务栏可能显示为默认空白图标（即使 --icon 和 setWindowIcon 都做了）。
    # 必须在 QApplication 创建【之前】调用；ID 取本项目唯一值即可。
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "E.V.ControlCenter")
        except Exception:
            pass  # 设置失败不致命，仅任务栏可能回退默认图标
    app = QApplication(sys.argv)
    if os.path.exists(ICON_FILE):
        app.setWindowIcon(QIcon(ICON_FILE))
    win = ControlCenter()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
