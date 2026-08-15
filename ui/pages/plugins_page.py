"""插件页（mixin）：卡片列表构建 / 配置页字段渲染 / 刷新。"""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QWidget

from src.utils import config
from ui.utils import env_helpers
from ui.utils.constants import PLUGIN_CONFIG_FIELDS
from ui.widgets.plugin_card import _PluginCard


class PluginsPage:
    """插件页逻辑：卡片列表 + 配置页（QStackedWidget 两页切换）。"""

    def _refresh_tools(self) -> None:
        """插件「刷新状态」：重读 .env / mcp_config.json 后重建卡片列表。"""
        config.reload_tool_runtime()
        self._fill_plugin_cards()
        self._log("[控制中心] 插件状态已刷新\n")

    def _init_tools_page(self) -> None:
        """初始化插件页：卡片列表 + 配置页（QStackedWidget 两页切换）。"""
        self._plugin_rows: list = []
        self._config_plugin_id: str = ""   # 当前配置页对应插件 id（空 = 未打开）
        self._config_field_rows: list = []
        self._config_mcp_editor = None     # MCP 配置页的 JSON 编辑器
        # 外部服务进程（插件页进程托管：mindcraft）：service_id → QProcess
        self._svc_procs: dict = {}
        # 卡片列表容器（滚动区内竖向排布，顶部对齐）
        self._plugin_list_layout = self.vlayout_plugin_cards
        self._plugin_list_layout.setAlignment(Qt.AlignTop)
        # 配置页：顶部返回 = 切回列表；保存 = 写 .env / mcp_config.json 后返回
        self.btn_plugin_back.clicked.connect(self._close_plugin_config)
        self.btn_save_plugin_config.clicked.connect(self._save_plugin_config)
        self._fill_plugin_cards()

    def _fill_plugin_cards(self) -> None:
        """重建插件卡片列表（重读 .env / mcp_config.json）。

        重建时保留列表滚动位置；每张卡片 = 插件信息 + 启用切换按钮 +
        配置按钮（需要配置的插件才有），点击卡片本体同样切换启用/关闭。
        """
        vbar = self.scroll_plugin_cards.verticalScrollBar()
        scroll_pos = vbar.value() if vbar is not None else 0
        rows = self._tool_rows()
        self._plugin_rows = rows
        layout = self._plugin_list_layout
        while layout.count():
            item = layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        for idx, row in enumerate(rows):
            card = _PluginCard(row)
            card.toggle_requested.connect(
                lambda checked, i=idx: self._on_plugin_toggle(i, checked))
            card.config_requested.connect(
                lambda i=idx: self._open_plugin_config(i))
            layout.addWidget(card)
        layout.addStretch(1)  # 卡片不足一屏时顶部对齐，不撑满
        if vbar is not None:
            vbar.setValue(scroll_pos)

    def _open_plugin_config(self, idx: int) -> None:
        """打开指定插件的配置页：按插件渲染 .env 字段 / MCP 服务器 JSON。"""
        row = self._plugin_rows[idx]
        tid = row["id"]
        self._config_plugin_id = tid
        self.label_plugin_config_title.setText(f"配置 {row['name']}")
        # 清空上一插件的字段
        layout = self.vlayout_plugin_config
        while layout.count():
            item = layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._config_field_rows = []
        self._config_mcp_editor = None
        if tid.startswith("mcp_server:"):
            key = tid[len("mcp_server:"):]
            hint = QLabel("直接编辑该 MCP 服务器的 mcp_config.json 配置（JSON）")
            hint.setStyleSheet("color: rgb(120, 110, 100);")
            editor = QPlainTextEdit()
            editor.setPlainText(self._mcp_server_json(key))
            layout.addWidget(hint)
            layout.addWidget(editor)
            self._config_mcp_editor = editor
        else:
            for env_key, label in PLUGIN_CONFIG_FIELDS.get(tid, []):
                layout.addWidget(self._build_config_field(env_key, label))
        layout.addStretch(1)
        self.stack_tools.setCurrentIndex(1)

    def _build_config_field(self, env_key: str, label: str) -> QWidget:
        """构造一个配置字段行：标签 + 输入框（预填当前 .env 值）。"""
        wrap = QWidget()
        lay = QHBoxLayout(wrap)
        lay.setContentsMargins(0, 0, 0, 0)
        lbl = QLabel(label)
        lbl.setFixedWidth(190)
        editor = QLineEdit()
        editor.setText(str(getattr(self.cfg, env_key, "") or ""))
        editor.setPlaceholderText(env_key)
        lay.addWidget(lbl)
        lay.addWidget(editor, 1)
        self._config_field_rows.append((env_key, label, editor))
        return wrap

    def _save_plugin_config(self) -> None:
        """配置页「保存」：写回 .env / mcp_config.json 后返回列表并热通知。"""
        tid = self._config_plugin_id
        if not tid:
            return
        try:
            if tid.startswith("mcp_server:"):
                key = tid[len("mcp_server:"):]
                self._save_mcp_server_json(key)
                self._log(f"[控制中心] MCP 服务器配置已保存：{key}\n")
            else:
                for env_key, _label, editor in self._config_field_rows:
                    env_helpers._update_env(env_key, editor.text().strip())
                self._log(f"[控制中心] 插件配置已保存：{tid}\n")
        except (OSError, ValueError) as e:
            self._log(f"[控制中心] 保存插件配置失败：{e}\n")
            return
        # 本进程配置刷新 + 主程序运行中热通知（!tools）
        config.reload_tool_runtime()
        self._notify_main_tools()
        self._close_plugin_config()

    def _close_plugin_config(self) -> None:
        """配置页顶部「← 返回」：重建卡片（配置可能已变）并切回列表页。"""
        self.stack_tools.setCurrentIndex(0)
        self._fill_plugin_cards()
