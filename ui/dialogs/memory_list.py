"""记忆列表弹窗（MemoryListDialog）：列出该用户全部记忆，可逐条查看/删除。"""

from PySide6.QtCore import QEasingCurve, QPoint, QPropertyAnimation, Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QDialog, QFrame, QGraphicsDropShadowEffect, QHBoxLayout, QLabel,
    QMessageBox, QPushButton, QScrollArea, QVBoxLayout, QWidget,
)

from src.memory import memory
from src.memory.memory_graph import _display_user
from ui.dialogs.confirm import ConfirmDialog
from ui.dialogs.memory_detail import _make_type_badge, _memory_type_label


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
        shadow = QGraphicsDropShadowEffect(card)
        shadow.setBlurRadius(24)
        shadow.setOffset(0, 6)
        shadow.setColor(QColor(0, 0, 0, 60))
        card.setGraphicsEffect(shadow)

    def _rebuild_list(self) -> None:
        """重建列表：清空后按最新优先填入每条记忆行。"""
        while self._list_layout.count():
            item = self._list_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        if not self._memories:
            empty = QLabel("（该用户暂无记忆）", self._list_container)
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
        """单条记忆行：类型徽标 + 名称/内容/更新时间 + 删除按钮。"""
        row = QWidget(self._list_container)
        row.setObjectName("memRow")
        lay = QHBoxLayout(row)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(12)

        # 行首类型徽标（与图谱节点配色一致）
        track = str(node.get("track") or "memory")
        type_text, type_color = _memory_type_label(node, track)
        lay.addWidget(_make_type_badge(type_text, type_color, row))

        info = QVBoxLayout()
        info.setSpacing(3)
        name = str(node.get("name") or "").strip()
        if name:
            name_label = QLabel(name, row)
            name_label.setObjectName("rowName")
            info.addWidget(name_label)
        content = str(node.get("content") or "").strip()
        if content:
            content_label = QLabel(content, row)
            content_label.setObjectName("rowContent")
            content_label.setWordWrap(True)
            info.addWidget(content_label)
        updated = node.get("updated_at") or node.get("created_at") or ""
        if updated:
            meta = QLabel(
                "更新于 " + str(updated).replace("T", " ")[:19], row)
            meta.setObjectName("metaLabel")
            info.addWidget(meta)
        lay.addLayout(info, 1)

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

    def _apply_style(self) -> None:
        self.setStyleSheet(
            "QDialog { background: transparent; }"
            "#dialogCard { background-color: rgb(252, 250, 245);"
            " border-radius: 14px; }"
            "#titleLabel { color: rgb(40, 35, 25); font-size: 15px;"
            " font-weight: bold; font-family: \"微软雅黑\"; }"
            "#divider { background-color: rgba(140, 135, 125, 60); }"
            "#memRow { background-color: rgb(247, 244, 238);"
            " border-radius: 10px; }"
            "#rowName { color: rgb(40, 35, 25); font-size: 13px;"
            " font-weight: bold; font-family: \"微软雅黑\"; }"
            "#rowContent { color: rgb(70, 65, 55); font-size: 12px;"
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
