"""无边框窗口拖动：按住窗口空白区域即可拖动。"""

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtWidgets import QApplication, QWidget

from src.memory.memory_graph import MemoryGraphWidget


class _WindowDragFilter(QObject):
    """无边框窗口拖动：qApp 级事件过滤器，按住窗口空白区域即可拖动。

    交互控件（按钮/输入框/下拉框/文本框/列表/表格/滚动条等）不触发拖动；
    点击空白（QWidget/QLabel 等非交互区域）时调用系统级 startSystemMove()，
    由操作系统接管拖动（Windows 下实时跟随、事件不丢失，比手动 move 可靠）。
    """

    # 交互控件类型：点击不触发拖动
    _INTERACTIVE = (
        "QAbstractButton", "QAbstractSpinBox", "QComboBox", "QLineEdit",
        "QAbstractSlider", "QAbstractScrollArea",
    )

    def __init__(self, window, app) -> None:
        super().__init__(app)
        self._window = window
        app.installEventFilter(self)

    def eventFilter(self, obj, event):
        t = event.type()
        if t != QEvent.Type.MouseButtonPress or event.button() != Qt.MouseButton.LeftButton:
            return False
        if not isinstance(obj, QWidget) or obj.window() is not self._window:
            return False
        # 交互控件（含其子类）不触发拖动
        if self._inside_interactive(obj):
            return False
        try:
            handle = self._window.windowHandle()
            if handle is not None:
                handle.startSystemMove()
                return True
        except Exception:
            pass
        return False

    def _inside_interactive(self, obj) -> bool:
        """obj 自身或其任一祖先是否为交互控件。

        注意：点击 QTextEdit/QListWidget/QScrollArea 等滚动区内部时，鼠标
        事件落在其 viewport（普通 QWidget）上，只检查 obj 本身会漏掉——
        必须沿父链向上检查（否则点日志想复制内容却会把窗口拖走）。
        """
        node = obj
        while node is not None:
            if isinstance(node, self._interactive_classes()):
                return True
            # 记忆图谱自绘处理节点拖拽/空白平移：图谱内的按下事件
            # 必须交给控件自己，不能触发窗口拖动（否则拖节点变成拖窗口）
            if isinstance(node, MemoryGraphWidget):
                return True
            node = node.parentWidget()
        return False

    @staticmethod
    def _interactive_classes() -> tuple:
        """懒加载交互控件类型（含 QAbstractScrollArea 的 viewport 命中）。"""
        cls = []
        for name in _WindowDragFilter._INTERACTIVE:
            c = getattr(__import__("PySide6.QtWidgets", fromlist=[name]), name, None)
            if c is not None:
                cls.append(c)
        return tuple(cls)
