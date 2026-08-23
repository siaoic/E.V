"""角色卡片（_RoleCard）：对齐预览页 .role-card。

每个用户一张卡：圆点 + 用户名 + 记忆数胶囊 + 最新记忆摘要（2 行截断）。
点击 = 弹出该用户记忆列表弹窗（对齐预览页 openRole）。
"""

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout

from tools.memory.memory_graph import _display_user


class _RoleCard(QFrame):
    """角色卡片：列方向 head 行（圆点 + 用户名 + 计数）+ 摘要（2 行截断）。

    点击卡片本体发出 clicked(user) 信号，由记忆页连接弹出该用户记忆列表
    弹窗（对齐预览页 .role-card@click → openRole）。
    """

    clicked = Signal(str)  # 参数：user 标识

    def __init__(self, user: str, count: int, preview: str) -> None:
        super().__init__()
        self.setObjectName("role_card")
        self._user = user
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        lay = QVBoxLayout(self)
        # padding 16、gap 8 对齐预览页 .plugin-card / .role-card
        lay.setContentsMargins(16, 16, 16, 16)
        lay.setSpacing(8)

        # 1. role-head：圆点 + 用户名（撑满） + 计数胶囊
        head = QHBoxLayout()
        head.setSpacing(8)
        dot = QLabel(self)
        dot.setObjectName("role_dot")
        dot.setFixedSize(12, 12)
        head.addWidget(dot)
        name = QLabel(_display_user(user), self)
        name.setObjectName("role_user")
        head.addWidget(name, 1)
        count_label = QLabel(f"{count} 条", self)
        count_label.setObjectName("role_count")
        head.addWidget(count_label)
        lay.addLayout(head)

        # 2. role-preview：最新记忆摘要（2 行截断对齐预览页 -webkit-line-clamp:2）
        # QLabel 在 maximumHeight 限制下 wordWrap=True 会自动只显示前 2 行，
        # 超出文本被裁剪（不显示省略号，视觉接近 line-clamp）
        preview_label = QLabel(preview or "（暂无记忆）", self)
        preview_label.setObjectName("role_preview")
        preview_label.setWordWrap(True)
        # 12px 字号 × line-height 1.7 ≈ 20px/行，2 行约 40-44px
        preview_label.setMaximumHeight(44)
        lay.addWidget(preview_label, 1)

    def mouseReleaseEvent(self, event) -> None:
        # 点击卡片本体（空白区域）= 弹出该用户记忆列表
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self._user)
        super().mouseReleaseEvent(event)
