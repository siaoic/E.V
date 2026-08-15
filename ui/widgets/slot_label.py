"""情绪卡片内的拖放槽位（表情/动作通用）+ 情绪映射绑定值归一化。"""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel


def _as_list(value) -> list:
    """归一化情绪映射绑定值：字符串 → 单元素列表（兼容旧单值配置），列表原样。"""
    if not value:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value if v]
    return [str(value)]


class _SlotLabel(QLabel):
    """情绪卡片内的拖放槽位（表情/动作通用）：未绑定虚线占位，
    拖拽悬停高亮，放入即绑定。kind 决定样式（expr=紫 / motion=蓝）。"""

    def __init__(self, emotion: str, on_bind, mime: str, kind: str, parent=None):
        super().__init__(parent)
        self._emotion = emotion
        self._on_bind = on_bind
        self._mime = mime
        self._kind = kind
        self.setObjectName(f"slot_empty_{kind}")
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setAcceptDrops(True)
        self.setMinimumHeight(40)
        self.setWordWrap(True)

    def _set_drag_hover(self, on: bool) -> None:
        self.setProperty("drag", "true" if on else "false")
        self.style().unpolish(self)
        self.style().polish(self)

    def dragEnterEvent(self, e):
        if e.mimeData().hasFormat(self._mime):
            e.acceptProposedAction()
            self._set_drag_hover(True)
        else:
            e.ignore()

    def dragMoveEvent(self, e):
        if e.mimeData().hasFormat(self._mime):
            e.acceptProposedAction()
        else:
            e.ignore()

    def dragLeaveEvent(self, e):
        self._set_drag_hover(False)
        super().dragLeaveEvent(e)

    def dropEvent(self, e):
        self._set_drag_hover(False)
        if not e.mimeData().hasFormat(self._mime):
            return
        value = bytes(e.mimeData().data(self._mime)).decode("utf-8", "replace")
        self._on_bind(self._emotion, value)
        e.acceptProposedAction()

    def set_value(self, value: str, text: str) -> None:
        """更新绑定显示：已绑定显示内容，未绑定恢复虚线占位（objectName 切换样式）。"""
        self.setObjectName(f"slot_filled_{self._kind}" if value
                           else f"slot_empty_{self._kind}")
        self.setText(text)
        self.style().unpolish(self)
        self.style().polish(self)
