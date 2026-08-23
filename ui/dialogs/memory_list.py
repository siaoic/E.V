"""记忆列表弹窗（MemoryListDialog）：列出该用户全部记忆，可逐条查看/删除。"""

from PySide6.QtCore import QEasingCurve, QPoint, QPropertyAnimation, Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QDialog, QFrame, QGraphicsDropShadowEffect, QHBoxLayout, QLabel,
    QMessageBox, QPushButton, QScrollArea, QVBoxLayout, QWidget,
)

from tools.memory import memory
from tools.memory.memory_graph import _display_user
from ui.dialogs.confirm import ConfirmDialog
from ui.dialogs.memory_detail import MemoryDetailDialog, _memory_type_label


class _ClickableRow(QFrame):
    """列表行容器：点击行本体（非按钮）发出 clicked(node) 信号。

    对齐预览页 .role-item@click → openMemoryDetail；QPushButton 的
    mouseRelease 事件不传播到父 QFrame，所以删除按钮点击不会误触行点击。
    """

    clicked = Signal(object)  # 参数：node dict

    def __init__(self, node: dict, parent: QWidget) -> None:
        super().__init__(parent)
        self._node = node

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self._node)
        super().mouseReleaseEvent(event)


def _make_type_chip(text: str, color: QColor, parent: QWidget) -> QLabel:
    """列表行的胶囊式类型 chip（对齐预览页 .role-item .chip）：
    纯色背景 + 白字 + 999px 圆角，区别于详情弹窗的 _make_type_badge
    （半透明同色底 + 圆点+类型名）。"""
    chip = QLabel(text, parent)
    chip.setObjectName("rowChip")
    c = color
    chip.setStyleSheet(
        f"#rowChip {{ background: rgb({c.red()},{c.green()},{c.blue()});"
        f" color: #fff; border-radius: 999px;"
        f" padding: 2px 9px; font-size: 11px;"
        f" font-family: \"微软雅黑\"; }}")
    return chip


class MemoryListDialog(QDialog):
    """记忆列表弹窗（图谱只显示人名时，点击用户名圆弹出）：
    列出该用户全部记忆，每条带删除按钮，确认后删除并刷新列表。
    样式与记忆详情弹窗同款暖色卡片。"""

    deleted = Signal(str)  # 记忆删除成功（带 id；控制中心据此过滤并刷新）

    def __init__(self, user: str, memories: list[dict], parent=None) -> None:
        super().__init__(parent)
        self._user = user
        self._memories = list(memories)
        self.setWindowFlags(
            Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        # 非模态：父窗口仍可交互（对齐记忆详情弹窗）
        self.setModal(False)
        self.resize(600, 480)
        self.setMinimumSize(480, 320)
        self._build_ui()
        self._apply_style()

    def _build_ui(self) -> None:
        card = QFrame(self)
        self._card = card
        card.setObjectName("dialogCard")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(28, 28, 28, 28)
        outer.addWidget(card)

        lay = QVBoxLayout(card)
        lay.setContentsMargins(24, 22, 24, 18)
        lay.setSpacing(0)

        # 1. 标题：人名 + 记忆条数
        title = QLabel(
            f"{_display_user(self._user)}（{len(self._memories)} 条记忆）", card)
        title.setObjectName("titleLabel")
        title.setWordWrap(True)
        lay.addWidget(title)

        # 2. 分隔线
        lay.addSpacing(12)
        divider = QFrame(card)
        divider.setObjectName("divider")
        divider.setFixedHeight(1)
        lay.addWidget(divider)

        # 3. 滚动列表：每条记忆一行（名称 + 内容 + 更新时间 + 删除按钮）
        lay.addSpacing(10)
        scroll = QScrollArea(card)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._list_container = QWidget()
        self._list_layout = QVBoxLayout(self._list_container)
        self._list_layout.setContentsMargins(0, 0, 0, 0)
        self._list_layout.setSpacing(8)
        scroll.setWidget(self._list_container)
        lay.addWidget(scroll, 1)
        self._rebuild_list()

        # 4. 底部：关闭按钮
        lay.addSpacing(14)
        bottom = QHBoxLayout()
        close_btn = QPushButton("关闭", card)
        close_btn.setObjectName("closeBtn")
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.clicked.connect(self._fade_out_and_close)
        bottom.addStretch(1)
        bottom.addWidget(close_btn)
        lay.addLayout(bottom)

        # 限高：列表长时靠内部滚动，不超过屏幕 60%
        screen = self.screen()
        if screen is not None:
            self.setMaximumHeight(
                int(screen.availableGeometry().height() * 0.6))

        # 阴影（画在卡片上；四周 margins 已为阴影留空间）
        # 对齐预览页 .mem-modal-card box-shadow: 0 12px 32px rgba(0,0,0,0.22)
        shadow = QGraphicsDropShadowEffect(card)
        shadow.setBlurRadius(32)
        shadow.setOffset(0, 12)
        shadow.setColor(QColor(0, 0, 0, 56))
        card.setGraphicsEffect(shadow)

    def _rebuild_list(self) -> None:
        """重建列表：清空后按最新优先填入每条记忆行。"""
        while self._list_layout.count():
            item = self._list_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        if not self._memories:
            empty = QLabel("该角色暂无记忆", self._list_container)
            empty.setObjectName("metaLabel")
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._list_layout.addWidget(empty)
            return
        ordered = sorted(
            self._memories,
            key=lambda m: m.get("updated_at") or m.get("created_at") or "",
            reverse=True)
        for m in ordered:
            self._list_layout.addWidget(self._make_row(m))
        self._list_layout.addStretch(1)

    def _make_row(self, node: dict) -> QWidget:
        """单条记忆行（对齐预览页 .role-item）：胶囊式类型 chip + 名称/摘要 +
        右侧更新时间 + 删除按钮，整行单排布。点击行本体弹详情弹窗。"""
        row = _ClickableRow(node, self._list_container)
        row.setObjectName("memRow")
        row.clicked.connect(self._show_detail_dialog)
        lay = QHBoxLayout(row)
        # padding 10/8/10/8、gap 10 对齐预览页 .role-item
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(10)

        # 行首：胶囊式类型 chip（纯色背景 + 白字，区别于详情弹窗的圆点徽标）
        track = str(node.get("track") or "memory")
        type_text, type_color = _memory_type_label(node, track)
        lay.addWidget(_make_type_chip(type_text, type_color, row))

        # 中部：名称（上） + 摘要（下），均单行 ellipsis 截断
        # （对齐预览页 .role-item-name / .role-item-snippet 的 nowrap + ellipsis）
        info = QVBoxLayout()
        info.setSpacing(2)
        name = str(node.get("name") or "").strip()
        if name:
            name_label = QLabel(name, row)
            name_label.setObjectName("rowName")
            # 单行截断对齐预览页 .role-item-name nowrap
            name_label.setWordWrap(False)
            info.addWidget(name_label)
        content = str(node.get("content") or "").strip()
        if content:
            content_label = QLabel(content, row)
            content_label.setObjectName("rowContent")
            # 单行截断对齐预览页 .role-item-snippet nowrap
            content_label.setWordWrap(False)
            info.addWidget(content_label)
        lay.addLayout(info, 1)

        # 右侧：更新时间紧凑显示（仅 yyyy-MM-dd HH:MM 对齐 .m-time 11px）
        updated = node.get("updated_at") or node.get("created_at") or ""
        if updated:
            meta = QLabel(str(updated).replace("T", " ")[:16], row)
            meta.setObjectName("metaLabel")
            lay.addWidget(meta)

        # 删除按钮
        delete_btn = QPushButton("删除", row)
        delete_btn.setObjectName("deleteBtn")
        delete_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        node_id = str(node.get("id") or "")
        if node_id:
            delete_btn.clicked.connect(
                lambda _=False, nid=node_id, nm=name or "（无名称）":
                self._on_delete_clicked(nid, nm))
        else:
            delete_btn.setEnabled(False)  # 无 id 的条目不可删
        lay.addWidget(delete_btn)
        return row

    def _on_delete_clicked(self, node_id: str, name: str) -> None:
        """删除单条记忆：二次确认 → 删除 → 刷新列表并发 deleted 信号。"""
        dlg = ConfirmDialog(
            self, "删除记忆",
            f"确定删除这条记忆？\n\n{name}\n\n删除后不可恢复。",
            confirm_text="删除")
        if dlg.exec() != ConfirmDialog.DialogCode.Accepted:
            return
        try:
            mm = memory.get_manager()
            n = mm.delete_memories([node_id])
        except Exception as e:
            QMessageBox.warning(
                self, "删除失败", f"删除记忆失败：{type(e).__name__}: {e}")
            return
        if not n:
            QMessageBox.warning(
                self, "删除失败",
                "记忆删除失败（存储可能被占用或损坏）。\n"
                "可尝试先停止主程序后重试。")
            return
        self._memories = [
            m for m in self._memories if str(m.get("id")) != node_id]
        self._rebuild_list()
        self.deleted.emit(node_id)

    def _show_detail_dialog(self, node: dict) -> None:
        """列表行点击：弹出该条记忆详情弹窗（对齐预览页 openMemoryDetail）。

        详情弹窗的 parent 是本列表弹窗（叠加在上层）；删除成功后从本列表
        的 _memories 过滤并重建，同时转发 deleted 信号给控制中心刷新网格。
        """
        if getattr(self, "_detail_dlg", None) is not None:
            self._detail_dlg.close()  # 先关旧详情弹窗（避免叠加）
        dlg = MemoryDetailDialog(node, self)
        dlg.deleted.connect(self._on_detail_deleted)
        self._detail_dlg = dlg
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()

    def _on_detail_deleted(self, node_id: str) -> None:
        """详情弹窗内删除成功：从列表过滤 + 重建 + 转发给控制中心刷新网格。"""
        self._memories = [
            m for m in self._memories if str(m.get("id")) != node_id]
        self._rebuild_list()
        self.deleted.emit(node_id)

    def _apply_style(self) -> None:
        self.setStyleSheet(
            "QDialog { background: transparent; }"
            # 卡片背景对齐预览页 .mem-modal-card: var(--bg-page) #fbf6ea +
            # 1px border-l2 + 14px 圆角
            "#dialogCard { background-color: rgb(251, 246, 234);"
            " border: 1px solid rgba(200, 185, 158, 130);"
            " border-radius: 14px; }"
            "#titleLabel { color: rgb(40, 35, 25); font-size: 15px;"
            " font-weight: bold; font-family: \"微软雅黑\"; }"
            # 分隔线对齐预览页 .mem-divider: var(--border-l2) 0.5 alpha
            "#divider { background-color: rgba(200, 185, 158, 128); }"
            # 行：layer-2 米色 + 1px border-l1 + 8px 圆角对齐预览页 .role-item
            "#memRow { background-color: rgba(246, 241, 231, 199);"
            " border: 1px solid rgba(200, 185, 158, 90);"
            " border-radius: 8px; }"
            # 悬停描边对齐预览页 .role-item:hover border-color: var(--brand-primary)
            "#memRow:hover { border: 1px solid rgba(120, 200, 188, 230); }"
            "#rowName { color: rgb(40, 35, 25); font-size: 13px;"
            " font-weight: bold; font-family: \"微软雅黑\"; }"
            # 摘要 11px 次级色对齐预览页 .role-item-snippet label-dimmed
            "#rowContent { color: rgb(120, 110, 100); font-size: 11px;"
            " font-family: \"微软雅黑\"; }"
            "#metaLabel { color: rgb(160, 155, 145); font-size: 11px;"
            " font-family: \"微软雅黑\"; }"
            "#closeBtn { background-color: rgb(237, 232, 220);"
            " color: rgb(114, 95, 77); border: none; border-radius: 10px;"
            " padding: 6px 22px; font-size: 13px;"
            " font-family: \"微软雅黑\"; }"
            "#closeBtn:hover { background-color: rgb(228, 220, 203); }"
            "#closeBtn:pressed { background-color: rgb(218, 208, 190); }"
            "#deleteBtn { background-color: rgba(196, 86, 76, 30);"
            " color: rgb(176, 70, 62); border: none; border-radius: 8px;"
            " padding: 4px 14px; font-size: 12px;"
            " font-family: \"微软雅黑\"; }"
            "#deleteBtn:hover { background-color: rgba(196, 86, 76, 55); }"
            "#deleteBtn:pressed { background-color: rgba(196, 86, 76, 80); }"
            # 滚动条隐藏（列表滚轮仍可滚动，视觉清爽）
            "QScrollBar:vertical { background: transparent; width: 0;"
            " margin: 0; }"
            "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical,"
            " QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical,"
            " QScrollBar::handle:vertical { width: 0; height: 0;"
            " background: transparent; }"
        )

    # 入场 / 出场动效（对齐记忆详情弹窗）
    _FADE_MS = 240
    _FADE_OUT_MS = 160

    def _play_show(self) -> None:
        """入场：淡入 0→1 + 自下而上 14px，240ms OutCubic。"""
        geo = self.geometry()
        end_pos = geo.topLeft()
        start_pos = QPoint(end_pos.x(), end_pos.y() + 14)
        self.move(start_pos)
        self._fade = QPropertyAnimation(self, b"windowOpacity", self)
        self._fade.setDuration(self._FADE_MS)
        self._fade.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._fade.setStartValue(0.0)
        self._fade.setEndValue(1.0)
        self._fade.start()

    def _fade_out_and_close(self) -> None:
        """出场：淡出 1→0，160ms 后关闭。"""
        out = QPropertyAnimation(self, b"windowOpacity", self)
        out.setDuration(self._FADE_OUT_MS)
        out.setEasingCurve(QEasingCurve.Type.InCubic)
        out.setStartValue(1.0)
        out.setEndValue(0.0)
        out.finished.connect(self.accept)
        out.start()

    def mousePressEvent(self, e) -> None:
        """点击卡片外（阴影呼吸区 / 透明背景）→ 关闭弹窗。"""
        if not getattr(self, "_card", None) \
                or not self._card.geometry().contains(e.position().toPoint()):
            self._fade_out_and_close()
            e.accept()
            return
        super().mousePressEvent(e)

    def showEvent(self, e) -> None:
        """首次显示时播放入场动画。"""
        super().showEvent(e)
        if not getattr(self, "_animated", False):
            self._animated = True
            self._play_show()
