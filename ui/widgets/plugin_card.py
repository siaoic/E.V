"""插件卡片：名称/类型徽标/状态/说明 + 启用切换按钮 + 配置按钮。"""

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout

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
    """插件卡片：名称/类型徽标/状态/说明 + 启用切换按钮 + 配置按钮。

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
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(10)
        # 左侧信息区：名称 + 类型徽标 / 状态 / 说明
        info = QVBoxLayout()
        info.setSpacing(4)
        head = QHBoxLayout()
        head.setSpacing(8)
        name = QLabel(row["name"])
        name.setObjectName("plugin_card_name")
        kind = QLabel(row["kind"])
        kind.setObjectName("plugin_card_kind")
        head.addWidget(name)
        head.addWidget(kind)
        head.addStretch(1)
        status = QLabel()
        status.setObjectName("plugin_card_status")
        status.setTextFormat(Qt.TextFormat.RichText)
        status.setText(_status_html(row))
        desc = QLabel(row["desc"])
        desc.setObjectName("plugin_card_desc")
        info.addLayout(head)
        info.addWidget(status)
        info.addWidget(desc)
        lay.addLayout(info, 1)
        # 右侧按钮区：启用/关闭切换 + 配置（需要配置的插件才显示）
        toggle = QPushButton("关闭" if row["enabled"] else "启用")
        toggle.setObjectName("plugin_card_toggle")
        # 注意：不能用属性名 enabled——与 Qt 内建 enabled 属性冲突，
        # QSS 选择器会读到真实启用状态；用 active 标识插件开关状态
        toggle.setProperty("active", "true" if row["enabled"] else "false")
        toggle.setEnabled(row["checkable"])
        toggle.clicked.connect(self._on_toggle_clicked)
        lay.addWidget(toggle)
        if (row["id"] in PLUGIN_CONFIG_FIELDS
                or row["id"].startswith("mcp_server:")):
            btn_config = QPushButton("配置")
            btn_config.setObjectName("plugin_card_config")
            btn_config.clicked.connect(self.config_requested.emit)
            lay.addWidget(btn_config)

    def _on_toggle_clicked(self) -> None:
        if not self._checkable:
            return
        self.toggle_requested.emit(not self._enabled)

    def mouseReleaseEvent(self, event) -> None:
        # 点击卡片空白区域 = 切换启用/关闭（按钮点击不会走到这里）
        if (event.button() == Qt.MouseButton.LeftButton and self._checkable):
            self.toggle_requested.emit(not self._enabled)
        super().mouseReleaseEvent(event)
