"""窗口关闭确认：Alt+F4 / 任务栏关闭前先弹确认框。"""

from PySide6.QtCore import QEvent, QObject
from PySide6.QtWidgets import QDialog

from ui.dialogs.confirm import _ConfirmDialog


class _CloseConfirmFilter(QObject):
    """关闭确认：窗口被关闭（Alt+F4 / 任务栏关闭）时先弹确认框。

    本窗口是无边框（FramelessWindowHint），没有系统 X 按钮，但 Alt+F4 /
    任务栏右键关闭等仍会触发 QCloseEvent。确认后放行，取消则忽略事件。
    过滤器只装在窗口上（不装 qApp 级，避免记忆详情弹窗等子窗口关闭时误弹）。
    """

    def __init__(self, window) -> None:
        super().__init__(window)
        self._window = window

    def eventFilter(self, obj, event) -> bool:
        if obj is not self._window or event.type() != QEvent.Type.Close:
            return False
        dlg = _ConfirmDialog(self._window)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            event.ignore()  # 取消：吞掉关闭事件，窗口保持打开
            return True
        return False  # 确认：放行默认关闭流程
