"""插件卡片：列方向布局对齐预览页 .plugin-card。

层级：name 行（名称 + 类型徽章）→ 状态 → 描述 → 底部 actions 行（配置 + 启用切换）。
"""

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout)

from ui.utils.constants import PLUGIN_CONFIG_FIELDS


def _status_html(row: dict) -> str:
    """状态文字着色：已启用绿 / 未配置橙 / 已关闭灰（对齐旧表格配色）。"""
    if row["status"].startswith("✅"):
        color = "#2d824b"
    elif row["status"].startswith("⚠️"):
        color = "#c8781e"
    else:
        color = "#828282"
    return f'<span style="color:{color};">{row["status"]}</span>'


class _PluginCard(QFrame):
    """插件卡片：列方向布局对齐预览页 control-center-preview.html 的 .plugin-card。

    层级：name 行（名称 + 类型徽章）→ 状态 → 描述 → 底部 actions 行（配置 + 启用切换）。
    点击卡片本体 = 切换启用/关闭（保留旧表格「点行即切换」交互）；
    子控件按钮会吞掉自己的鼠标事件，不会误触卡片的切换逻辑。
    """

    toggle_requested = Signal(bool)  # 参数：目标启用状态
    config_requested = Signal()

    def __init__(self, row: dict) -> None:
        super().__init__()
        self.setObjectName("plugin_card")
        self.setProperty("checkable", "true" if row["checkable"] else "false")
        self._enabled = row["enabled"]
        self._checkable = row["checkable"]
        lay = QVBoxLayout(self)
        # padding 16px、gap 8px 对齐预览页 .plugin-card
        lay.setContentsMargins(16, 16, 16, 16)
        lay.setSpacing(8)

        # 1. name 行：名称（左撑满）+ 类型徽章（右靠）
        head = QHBoxLayout()
        head.setSpacing(8)
        name = QLabel(row["name"])
        name.setObjectName("plugin_card_name")
        # 名称过长时单行截断（wordWrap=False 时 QLabel 会按 layout 限制宽度裁剪）
        name.setWordWrap(False)
        head.addWidget(name, 1)
        kind = QLabel(row["kind"])
        kind.setObjectName("plugin_card_kind")
        head.addWidget(kind)
        lay.addLayout(head)

        # 2. 状态文字（着色由 _status_html 内联 span 决定）
        status = QLabel()
        status.setObjectName("plugin_card_status")
        status.setTextFormat(Qt.TextFormat.RichText)
        status.setText(_status_html(row))
        lay.addWidget(status)

        # 3. 描述（垂直撑满，把 actions 永远推到底部）
        desc = QLabel(row["desc"])
        desc.setObjectName("plugin_card_desc")
        desc.setWordWrap(True)
        lay.addWidget(desc, 1)

        # 4. 底部 actions 行：靠右排布，配置在左、启用切换在右
        actions = QHBoxLayout()
        actions.setSpacing(8)
        actions.addStretch(1)
        if (row["id"] in PLUGIN_CONFIG_FIELDS
                or row["id"].startswith("mcp_server:")):
            btn_config = QPushButton("配置")
            btn_config.setObjectName("plugin_card_config")
            btn_config.clicked.connect(self.config_requested.emit)
            actions.addWidget(btn_config)
        toggle = QPushButton("关闭" if row["enabled"] else "启用")
        toggle.setObjectName("plugin_card_toggle")
        # 注意：不能用属性名 enabled——与 Qt 内建 enabled 属性冲突，
        # QSS 选择器会读到真实启用状态；用 active 标识插件开关状态
        toggle.setProperty("active", "true" if row["enabled"] else "false")
        toggle.setEnabled(row["checkable"])
        toggle.clicked.connect(self._on_toggle_clicked)
        actions.addWidget(toggle)
        lay.addLayout(actions)

    def _on_toggle_clicked(self) -> None:
        if not self._checkable:
            return
        self.toggle_requested.emit(not self._enabled)

    def mouseReleaseEvent(self, event) -> None:
        # 点击卡片空白区域 = 切换启用/关闭（按钮点击不会走到这里）
        if (event.button() == Qt.MouseButton.LeftButton and self._checkable):
            self.toggle_requested.emit(not self._enabled)
        super().mouseReleaseEvent(event)
