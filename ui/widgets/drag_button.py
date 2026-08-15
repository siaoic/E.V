"""可拖拽按钮基类：点击试播 + 按住拖动发绑定拖拽。"""

from PySide6.QtCore import QEvent, QMimeData, Qt
from PySide6.QtGui import QDrag
from PySide6.QtWidgets import QApplication, QPushButton


class _DragButton(QPushButton):
    """可拖拽按钮基类：点击试播 + 按住拖动发绑定拖拽。

    表情按钮（_DRAG_MIME_EXPR，橙粉渐变）与动作卡片（_DRAG_MIME_MOTION，
    蓝色渐变）共用拖拽交互，仅 mime/样式 objectName 不同；由上方 _SlotLabel
    按 mime 类型接收并绑到对应槽位。
    """

    def __init__(self, value: str, on_preview, mime: str, object_name: str,
                 on_drag_finished=None, parent=None):
        super().__init__(parent)
        self.value = value
        self._on_preview = on_preview
        self._mime = mime
        self._on_drag_finished = on_drag_finished
        self._press_pos = None
        self._dragging = False
        self.setObjectName(object_name)
        self.setText(value)
        self.clicked.connect(lambda: self._on_preview(self.value))

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._press_pos = e.position().toPoint()
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if (self._press_pos is not None and not self._dragging
                and (e.position().toPoint() - self._press_pos).manhattanLength()
                >= QApplication.startDragDistance()):
            self._dragging = True
            self.setDown(False)          # 拖动不算点击，避免拖完触发试播
            self._begin_drag()
            self._dragging = False
            self._press_pos = None
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        self._press_pos = None
        super().mouseReleaseEvent(e)

    def _begin_drag(self) -> None:
        md = QMimeData()
        md.setData(self._mime, self.value.encode("utf-8"))
        drag = QDrag(self)
        drag.setMimeData(md)
        drag.setPixmap(self.grab())
        drag.setHotSpot(drag.pixmap().rect().center())
        drag.exec(Qt.DropAction.MoveAction)
        # 拖拽结束（drag.exec 返回）后统一刷新按钮可见性：Qt 规范禁止在
        # 拖拽进行中（dropEvent 属于 drag.exec 阻塞循环）修改源 widget 的
        # 可见性，否则布局刷新被吞、按钮「又显示出来」。
        if self._on_drag_finished is not None:
            self._on_drag_finished()
        # 清理拖拽残留状态：拖动期间按钮收不到 Leave 事件，WA_UnderMouse
        # 残留 true → 之后显示时 QSS :hover 样式残留，按钮看起来「变暗」
        self.setDown(False)
        QApplication.sendEvent(self, QEvent(QEvent.Type.Leave))
