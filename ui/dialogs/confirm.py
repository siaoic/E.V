"""确认弹窗：通用确认（ConfirmDialog）+ 窗口关闭确认（_ConfirmDialog）。"""

from PySide6.QtCore import QEasingCurve, QPoint, QPropertyAnimation, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QDialog, QFrame, QGraphicsDropShadowEffect, QHBoxLayout, QLabel,
    QPushButton, QVBoxLayout,
)


class ConfirmDialog(QDialog):
    """通用确认弹窗：与记忆详情弹窗同款暖色卡片风格。

    无边框圆角米白卡片 + 阴影 + 淡入上移动画；标题 / 正文 / 按钮文字
    可配，confirm 按钮支持危险色（红）或常规色。exec() 返回 Accepted /
    Rejected（确认/取消都有淡出动画，完成后才真正关闭）。

    用法：
        dlg = ConfirmDialog(self, "删除记忆", "确定删除？", confirm_text="删除")
        if dlg.exec() == ConfirmDialog.DialogCode.Accepted:
            ...
    """

    _FADE_MS = 240
    _FADE_OUT_MS = 160
    _RISE_PX = 14

    def __init__(self, parent, title: str, message: str,
                 confirm_text: str = "确定", cancel_text: str = "取消",
                 danger: bool = True) -> None:
        super().__init__(parent)
        self.setWindowFlags(
            Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setModal(True)
        self.resize(420, 220)
        self.setMinimumSize(380, 190)
        self._build_ui(title, message, confirm_text, cancel_text, danger)
        self._apply_style()

    def _build_ui(self, title: str, message: str,
                  confirm_text: str, cancel_text: str, danger: bool) -> None:
        card = QFrame(self)
        card.setObjectName("dialogCard")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(28, 28, 28, 28)
        outer.addWidget(card)

        lay = QVBoxLayout(card)
        lay.setContentsMargins(26, 24, 26, 20)
        lay.setSpacing(0)

        t = QLabel(title, card)
        t.setObjectName("titleLabel")
        t.setWordWrap(True)
        lay.addWidget(t)

        divider = QFrame(card)
        divider.setObjectName("divider")
        divider.setFixedHeight(1)
        lay.addSpacing(14)
        lay.addWidget(divider)

        msg = QLabel(message, card)
        msg.setObjectName("descLabel")
        msg.setWordWrap(True)
        msg.setAlignment(Qt.AlignmentFlag.AlignTop)
        lay.addSpacing(12)
        lay.addWidget(msg, 1)

        btns = QHBoxLayout()
        btns.setSpacing(8)
        btns.addStretch(1)
        cancel_btn = QPushButton(cancel_text, card)
        cancel_btn.setObjectName("closeBtn")
        cancel_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        cancel_btn.clicked.connect(self._on_cancel)
        btns.addWidget(cancel_btn)
        confirm_btn = QPushButton(confirm_text, card)
        confirm_btn.setObjectName("deleteBtn" if danger else "closeBtn")
        confirm_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        confirm_btn.setDefault(True)
        confirm_btn.clicked.connect(self._on_confirm)
        self._confirm_btn = confirm_btn
        btns.addWidget(confirm_btn)
        lay.addSpacing(16)
        lay.addLayout(btns)

        shadow = QGraphicsDropShadowEffect(card)
        shadow.setBlurRadius(24)
        shadow.setOffset(0, 6)
        shadow.setColor(QColor(0, 0, 0, 60))
        card.setGraphicsEffect(shadow)

    def _apply_style(self) -> None:
        self.setStyleSheet(
            "QDialog { background: transparent; }"
            "#dialogCard { background-color: rgb(252, 250, 245);"
            " border-radius: 14px; }"
            "#titleLabel { color: rgb(40, 35, 25); font-size: 15px;"
            " font-weight: bold; font-family: \"微软雅黑\"; }"
            "#descLabel { color: rgb(70, 65, 55); font-size: 13px;"
            " font-family: \"微软雅黑\"; }"
            "#divider { background-color: rgba(140, 135, 125, 60); }"
            "#closeBtn { background-color: rgb(237, 232, 220);"
            " color: rgb(114, 95, 77); border: none; border-radius: 10px;"
            " padding: 6px 24px; font-size: 13px;"
            " font-family: \"微软雅黑\"; }"
            "#closeBtn:hover { background-color: rgb(228, 220, 203); }"
            "#closeBtn:pressed { background-color: rgb(218, 208, 190); }"
            "#deleteBtn { background-color: rgba(196, 86, 76, 30);"
            " color: rgb(176, 70, 62); border: none; border-radius: 10px;"
            " padding: 6px 24px; font-size: 13px;"
            " font-family: \"微软雅黑\"; }"
            "#deleteBtn:hover { background-color: rgba(196, 86, 76, 55); }"
            "#deleteBtn:pressed { background-color: rgba(196, 86, 76, 80); }"
        )

    def _on_confirm(self) -> None:
        self._fade_out_and_close(self.accept)

    def _on_cancel(self) -> None:
        self._fade_out_and_close(self.reject)

    def _fade_out_and_close(self, done) -> None:
        """淡出后调用 done（accept / reject），动画期间事件循环仍处理 exec。"""
        out = QPropertyAnimation(self, b"windowOpacity", self)
        out.setDuration(self._FADE_OUT_MS)
        out.setEasingCurve(QEasingCurve.Type.InCubic)
        out.setStartValue(1.0)
        out.setEndValue(0.0)
        out.finished.connect(done)
        out.start()

    def _play_show(self) -> None:
        geo = self.geometry()
        end_pos = geo.topLeft()
        start_pos = QPoint(end_pos.x(), end_pos.y() + self._RISE_PX)
        self.move(start_pos)
        self.setWindowOpacity(0.0)
        self._rise = QPropertyAnimation(self, b"pos", self)
        self._rise.setDuration(self._FADE_MS)
        self._rise.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._rise.setStartValue(start_pos)
        self._rise.setEndValue(end_pos)
        self._rise.start()
        self._fade = QPropertyAnimation(self, b"windowOpacity", self)
        self._fade.setDuration(self._FADE_MS)
        self._fade.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._fade.setStartValue(0.0)
        self._fade.setEndValue(1.0)
        self._fade.start()

    def showEvent(self, e) -> None:
        super().showEvent(e)
        # 窗口真正显示后再设焦点（__init__ 里 setFocus 会被 Qt 忽略，
        # 导致默认按钮高亮不生效）；setDefault(True) 已保证回车触发确认。
        self._confirm_btn.setFocus()
        if not getattr(self, "_animated", False):
            self._animated = True
            self._play_show()


class _ConfirmDialog(QDialog):
    """确认关闭弹窗：样式与「记忆详情弹窗」（MemoryDetailDialog）完全一致。

    复用同一套视觉语言：米白圆角卡片 + 深棕标题 + 棕正文 + 分隔线 +
    底部按钮（取消=米灰 closeBtn / 确认关闭=红系 deleteBtn），
    阴影 blur 24 / (0, 6) / 黑色 24% 透明度。exec() 返回
    Accepted（确认）或 Rejected（取消）。
    """

    def __init__(self, parent=None, title: str = "确认关闭",
                 text: str = "确定要关闭控制中心吗？") -> None:
        super().__init__(parent)
        self.setWindowFlags(
            Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.resize(360, 196)
        self.setMinimumSize(320, 160)

        # 卡片容器：圆角米白背景 + 阴影（四周 margins 为阴影留呼吸空间）
        card = QFrame(self)
        card.setObjectName("dialogCard")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(28, 28, 28, 28)
        outer.addWidget(card)

        lay = QVBoxLayout(card)
        lay.setContentsMargins(24, 22, 24, 18)
        lay.setSpacing(0)

        # 1. 标题
        title_label = QLabel(title, card)
        title_label.setObjectName("titleLabel")
        title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(title_label)

        # 2. 正文（居中，可换行）
        lay.addSpacing(12)
        body = QLabel(text, card)
        body.setObjectName("descLabel")
        body.setAlignment(Qt.AlignmentFlag.AlignCenter)
        body.setWordWrap(True)
        lay.addWidget(body, 1)

        # 3. 分隔线
        lay.addSpacing(14)
        divider = QFrame(card)
        divider.setObjectName("divider")
        divider.setFixedHeight(1)
        lay.addWidget(divider)

        # 4. 底部按钮行：取消靠最左、确认关闭靠最右（两端对齐）
        bottom = QHBoxLayout()
        bottom.setSpacing(16)
        btn_cancel = QPushButton("取消", card)
        btn_cancel.setObjectName("closeBtn")
        btn_cancel.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_cancel.clicked.connect(self.reject)
        btn_ok = QPushButton("确认关闭", card)
        btn_ok.setObjectName("deleteBtn")
        btn_ok.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_ok.setDefault(True)
        btn_ok.clicked.connect(self.accept)
        bottom.addWidget(btn_cancel)
        bottom.addStretch(1)
        bottom.addWidget(btn_ok)
        lay.addSpacing(14)
        lay.addLayout(bottom)

        # 阴影（与记忆弹窗一致）
        shadow = QGraphicsDropShadowEffect(card)
        shadow.setBlurRadius(24)
        shadow.setOffset(0, 6)
        shadow.setColor(QColor(0, 0, 0, 60))
        card.setGraphicsEffect(shadow)

        # 样式逐值与 MemoryDetailDialog._apply_style 保持一致
        self.setStyleSheet(
            "QDialog { background: transparent; }"
            "#dialogCard { background-color: rgb(252, 250, 245);"
            " border-radius: 14px; }"
            "#titleLabel { color: rgb(40, 35, 25); font-size: 15px;"
            " font-weight: bold; font-family: \"微软雅黑\"; }"
            "#descLabel { color: rgb(114, 95, 77); font-size: 13px;"
            " font-family: \"微软雅黑\"; }"
            "#divider { background-color: rgba(140, 135, 125, 60); }"
            "#closeBtn { background-color: rgb(237, 232, 220);"
            " color: rgb(114, 95, 77); border: none; border-radius: 10px;"
            " padding: 6px 22px; font-size: 13px;"
            " font-family: \"微软雅黑\"; }"
            "#closeBtn:hover { background-color: rgb(228, 220, 203); }"
            "#closeBtn:pressed { background-color: rgb(218, 208, 190); }"
            "#deleteBtn { background-color: rgba(196, 86, 76, 30);"
            " color: rgb(176, 70, 62); border: none; border-radius: 10px;"
            " padding: 6px 20px; font-size: 13px;"
            " font-family: \"微软雅黑\"; }"
            "#deleteBtn:hover { background-color: rgba(196, 86, 76, 55); }"
            "#deleteBtn:pressed { background-color: rgba(196, 86, 76, 80); }"
        )
