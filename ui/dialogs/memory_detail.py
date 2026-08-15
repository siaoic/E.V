"""记忆详情弹窗（MemoryDetailDialog）+ 记忆类型徽标工具。"""

from typing import Tuple

from PySide6.QtCore import QEasingCurve, QPoint, QPropertyAnimation, Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QDialog, QFrame, QGraphicsDropShadowEffect, QHBoxLayout, QLabel,
    QMessageBox, QPlainTextEdit, QPushButton, QVBoxLayout, QWidget,
)

from src.memory import memory
from src.memory.memory_graph import _display_user
from ui.dialogs.confirm import ConfirmDialog
from ui.utils.constants import (
    _MEMORY_LAYER_COLORS, _MEMORY_LAYER_NAMES,
    _MEMORY_TRACK_COLORS, _MEMORY_TRACK_NAMES,
)


def _memory_type_label(node: dict, track: str) -> Tuple[str, QColor]:
    """记忆类型徽标：(显示名, 主色)。配色与图谱节点一致——
    「实体记忆」（core/ 前缀三元组）优先，其次 layer 前缀，最后 track。"""
    desc = (node.get("description") or "").strip()
    # 实体记忆：agent 以「core/实体记忆：」落盘的结构化三元组事实，
    # 不是图谱语义层的「核心身份」，需单独识别
    if desc.startswith("core/实体记忆"):
        return "实体记忆", QColor(*_MEMORY_LAYER_COLORS["core"])
    prefix = desc.split("/")[0].strip().lower()
    if prefix in _MEMORY_LAYER_NAMES:
        r, g, b = _MEMORY_LAYER_COLORS[prefix]
        return _MEMORY_LAYER_NAMES[prefix], QColor(r, g, b)
    name = _MEMORY_TRACK_NAMES.get(track, track)
    if track in _MEMORY_TRACK_COLORS:
        r, g, b = _MEMORY_TRACK_COLORS[track]
        return name, QColor(r, g, b)
    return name, QColor(150, 150, 150)


def _make_type_badge(text: str, color: QColor, parent: QWidget) -> QFrame:
    """记忆类型徽标：彩色圆点 + 类型名，配色与图谱节点一致。"""
    badge = QFrame(parent)
    badge.setObjectName("typeBadge")
    row = QHBoxLayout(badge)
    row.setContentsMargins(9, 3, 11, 3)
    row.setSpacing(4)
    dot = QLabel("●", badge)
    dot.setObjectName("typeDot")
    label = QLabel(text, badge)
    label.setObjectName("typeText")
    row.addWidget(dot)
    row.addWidget(label)
    c = color
    badge.setStyleSheet(
        f"#typeBadge {{ background: rgba({c.red()},{c.green()},{c.blue()},28);"
        f" border-radius: 10px; }}"
        f"#typeDot {{ color: rgb({c.red()},{c.green()},{c.blue()});"
        f" font-size: 8px; }}"
        f"#typeText {{ color: rgb({c.red()},{c.green()},{c.blue()});"
        f" font-size: 12px; font-weight: bold;"
        f" font-family: \"微软雅黑\"; }}")
    return badge


class MemoryDetailDialog(QDialog):
    """记忆详情弹窗：无边框圆角卡片，暖色系对齐控制中心整体风格。

    信息层级自上而下：标题（记忆名）→ 归属/类型/层级标签（chips）
    → 一句话摘要 → 正文全文（内部滚动）→ 底部更新时间 + 关闭按钮。
    入场/出场带淡入 + 轻微上移动画，避免系统默认 QMessageBox 的突兀感。

    样式对齐现有 UI（ui/control_center.ui）：
      - 背景米白 rgb(252,250,245)、标题深棕 rgb(40,35,25)、
        正文棕 rgb(70,65,55)、次级灰棕 rgb(160,155,145)
      - 卡片圆角 14px、chips/按钮圆角 10px、微软雅黑 12-15px
      - 阴影 blur 24 / 偏移 (0, 6) / 黑色 24% 透明度
    """

    # 记忆层 → 标签色（与 src/memory/memory_graph.py _LAYER_STYLE 同义）
    _LAYER_COLORS = _MEMORY_LAYER_COLORS
    _LAYER_NAMES = _MEMORY_LAYER_NAMES

    # 动画时长（毫秒）：入场 240ms / 出场 160ms，OutCubic 缓动
    _FADE_MS = 240
    _FADE_OUT_MS = 160
    _RISE_PX = 14

    deleted = Signal(str)  # 记忆删除成功（带 id；控制中心据此过滤并刷新）

    def __init__(self, node: dict, parent=None) -> None:
        super().__init__(parent)
        self._node = node
        self.setWindowFlags(
            Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        # 非模态：父窗口（图谱）仍可交互——点击图谱空白可关闭本弹窗
        self.setModal(False)
        # 卡片外部留出阴影呼吸空间（阴影 blur 24，四周留 28px）
        self.resize(560, 420)
        self.setMinimumSize(440, 280)

        self._build_ui(node)
        self._apply_style()

    # ------------------------------------------------------------------
    # 界面构建
    # ------------------------------------------------------------------

    def _build_ui(self, node: dict) -> None:
        name = str(node.get("name") or "")
        desc = str(node.get("description") or "")
        content = str(node.get("content") or "").strip() or "（无正文内容）"
        user = str(node.get("user") or "chao")
        track = str(node.get("track") or "memory")
        updated = (node.get("updated_at") or node.get("created_at") or "")
        if updated:
            updated = updated.replace("T", " ")[:19]

        # 卡片容器：承载圆角米白背景 + 阴影
        card = QFrame(self)
        self._card = card
        card.setObjectName("dialogCard")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(28, 28, 28, 28)  # 阴影呼吸空间
        outer.addWidget(card)

        lay = QVBoxLayout(card)
        lay.setContentsMargins(24, 22, 24, 18)
        lay.setSpacing(0)

        # 1. 标题行：左上角类型徽标 + 记忆名 + 右上标签行
        title_row = QHBoxLayout()
        title_row.setSpacing(8)
        type_text, type_color = _memory_type_label(node, track)
        title_row.addWidget(_make_type_badge(type_text, type_color, card))
        title = QLabel(name or "记忆", card)
        title.setObjectName("titleLabel")
        title.setWordWrap(True)
        title_row.addWidget(title, 1)
        title_row.addStretch(0)
        chips = QHBoxLayout()
        chips.setSpacing(6)
        for text, color in self._chips(node, user, track):
            chips.addWidget(self._make_chip(text, color, card))
        title_row.addLayout(chips)
        lay.addLayout(title_row)

        # 2. 一句话摘要（有才显示）
        if desc:
            desc_label = QLabel(desc, card)
            desc_label.setObjectName("descLabel")
            desc_label.setWordWrap(True)
            lay.addSpacing(10)
            lay.addWidget(desc_label)

        # 3. 分隔线
        divider = QFrame(card)
        divider.setObjectName("divider")
        divider.setFixedHeight(1)
        lay.addSpacing(14)
        lay.addWidget(divider)

        # 4. 正文全文（只读，内部滚动，不把弹窗撑出屏幕）
        body = QPlainTextEdit(card)
        body.setObjectName("bodyEdit")
        body.setPlainText(content)
        body.setReadOnly(True)
        body.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        body.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        lay.addSpacing(12)
        lay.addWidget(body, 1)

        # 5. 底部行：更新时间 + 删除按钮（确认后删除）+ 关闭按钮
        bottom = QHBoxLayout()
        bottom.setSpacing(8)
        meta = QLabel(f"更新于 {updated}" if updated else " ", card)
        meta.setObjectName("metaLabel")
        bottom.addWidget(meta, 1)
        # 删除按钮仅对单条记忆显示（有 id）；用户汇总节点无 id 不显示
        if self._node.get("id"):
            delete_btn = QPushButton("删除", card)
            delete_btn.setObjectName("deleteBtn")
            delete_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            delete_btn.clicked.connect(self._on_delete_clicked)
            bottom.addWidget(delete_btn)
        close_btn = QPushButton("关闭", card)
        close_btn.setObjectName("closeBtn")
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.clicked.connect(self._fade_out_and_close)
        bottom.addWidget(close_btn)
        lay.addSpacing(14)
        lay.addLayout(bottom)

        # 限高：长正文靠内部滚动，不超过屏幕 60%
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

    def _chips(self, node: dict, user: str, track: str) -> list:
        """构建标签 chips：层级/实体（description 前缀）→ 归属（user）→ 类型（track）。"""
        chips = []
        # 层级标签（description 前缀 core/state/preference/archive）；
        # 「core/实体记忆」是三元组事实而非「核心身份」层，单独显示
        desc = (node.get("description") or "").strip()
        if desc.startswith("core/实体记忆"):
            chips.append(("实体记忆", QColor(*self._LAYER_COLORS["core"])))
        else:
            prefix = desc.split("/")[0].strip().lower()
            if prefix in self._LAYER_NAMES:
                r, g, b = self._LAYER_COLORS[prefix]
                chips.append((self._LAYER_NAMES[prefix], QColor(r, g, b)))
        # 归属标签：AI 自我（self）显示 AI 名字（neuro），其余为观众/用户 id
        chips.append((_display_user(user), QColor(140, 100, 200)
                      if user == "self" else QColor(60, 95, 160)))
        # 类型标签
        chips.append((track or "memory", QColor(150, 143, 130)))
        return chips

    @staticmethod
    def _make_chip(text: str, color: QColor, parent: QWidget) -> QLabel:
        """圆角小标签：半透明底色 + 同色系文字。"""
        chip = QLabel(text, parent)
        chip.setObjectName("chip")
        c = color
        chip.setStyleSheet(
            f"#chip {{ background: rgba({c.red()},{c.green()},{c.blue()},28);"
            f" color: rgb({c.red()},{c.green()},{c.blue()});"
            f" border-radius: 9px; padding: 2px 10px;"
            f" font-size: 12px; font-family: \"微软雅黑\"; }}")
        return chip

    # ------------------------------------------------------------------
    # 样式
    # ------------------------------------------------------------------

    def _apply_style(self) -> None:
        self.setStyleSheet(
            "QDialog { background: transparent; }"
            "#dialogCard { background-color: rgb(252, 250, 245);"
            " border-radius: 14px; }"
            "#titleLabel { color: rgb(40, 35, 25); font-size: 15px;"
            " font-weight: bold; font-family: \"微软雅黑\"; }"
            "#descLabel { color: rgb(114, 95, 77); font-size: 13px;"
            " font-family: \"微软雅黑\"; }"
            "#divider { background-color: rgba(140, 135, 125, 60); }"
            "#bodyEdit { background: transparent; border: none;"
            " color: rgb(70, 65, 55); font-size: 13px;"
            " font-family: \"微软雅黑\"; }"
            "#metaLabel { color: rgb(160, 155, 145); font-size: 12px;"
            " font-family: \"微软雅黑\"; }"
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
            # 滚动条隐藏（正文滚轮仍可滚动，视觉清爽）
            "QScrollBar:vertical { background: transparent; width: 0;"
            " margin: 0; }"
            "QScrollBar:horizontal { background: transparent; height: 0;"
            " margin: 0; }"
            "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical,"
            " QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical,"
            " QScrollBar::handle:vertical, QScrollBar::handle:horizontal,"
            " QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal"
            " { width: 0; height: 0; background: transparent; }"
        )

    # ------------------------------------------------------------------
    # 删除记忆
    # ------------------------------------------------------------------

    def _on_delete_clicked(self) -> None:
        """删除按钮：二次确认后删除该记忆，成功发 deleted 信号并关闭弹窗。"""
        node_id = self._node.get("id") or ""
        if not node_id:
            QMessageBox.warning(self, "无法删除", "该记忆缺少 id，无法删除。")
            return
        name = str(self._node.get("name") or "")
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
        if n:
            self.deleted.emit(node_id)  # 控制中心据此过滤并刷新
        else:
            # 删除完全失败（库异常/锁定等）：明确提示，避免「点了没反应」
            QMessageBox.warning(
                self, "删除失败",
                "记忆删除失败（存储可能被占用或损坏）。\n"
                "可尝试先停止主程序后重试。")
        self._fade_out_and_close()

    # ------------------------------------------------------------------
    # 入场 / 出场动效
    # ------------------------------------------------------------------

    def _play_show(self) -> None:
        """入场：淡入 0→1 + 自下而上 14px，240ms OutCubic（与阴影配合有浮起感）。"""
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

    def _fade_out_and_close(self) -> None:
        """出场：淡出 1→0，160ms 后关闭（避免系统默认瞬间消失的突兀感）。"""
        out = QPropertyAnimation(self, b"windowOpacity", self)
        out.setDuration(self._FADE_OUT_MS)
        out.setEasingCurve(QEasingCurve.Type.InCubic)
        out.setStartValue(1.0)
        out.setEndValue(0.0)
        out.finished.connect(self.accept)
        out.start()

    def mousePressEvent(self, e) -> None:
        """点击卡片外（阴影呼吸区 / 透明背景）→ 关闭弹窗（点击空白关闭）。"""
        if not getattr(self, "_card", None) \
                or not self._card.geometry().contains(e.position().toPoint()):
            self._fade_out_and_close()
            e.accept()
            return
        super().mousePressEvent(e)

    def showEvent(self, e) -> None:
        """首次显示时播放入场动画（exec 后触发，此前窗口已定位）。"""
        super().showEvent(e)
        if not getattr(self, "_animated", False):
            self._animated = True
            self._play_show()
