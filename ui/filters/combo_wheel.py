"""QComboBox 滚轮守卫：悬停时滚轮只滚列表，不误改选中值。"""

from PySide6.QtCore import QEvent, QObject
from PySide6.QtWidgets import QAbstractScrollArea, QApplication


class _ComboWheelGuard(QObject):
    """QComboBox 滚轮守卫：鼠标悬停（未点击）时滚轮会误改选中值（Qt 默认行为）。

    拦截 Wheel 事件并转交给最近的滚动区（QScrollArea 的 viewport），
    这样悬停在下拉框上滚动时，只会滚动所在的列表/区域，不会误改值。
    """

    def eventFilter(self, obj, event):
        if event.type() != QEvent.Type.Wheel:
            return False
        # 转给父链上最近的滚动区滚动；combo 自身不再处理滚轮
        node = obj.parentWidget()
        while node is not None:
            if isinstance(node, QAbstractScrollArea):
                QApplication.sendEvent(node.viewport(), event)
                return True
            node = node.parentWidget()
        return True
