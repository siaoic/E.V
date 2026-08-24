"""记忆页（mixin）：角色卡片网格 / 记忆详情与列表弹窗 / 删除刷新。

对齐预览页 control-center-preview.html 的 .role-grid：每个用户一张卡，
3 列网格排布，点击卡片弹出该用户记忆列表弹窗（详见 MemoryListDialog）。
"""

import json
import os

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QScrollArea, QSizePolicy, QVBoxLayout, QWidget)

from tools.memory import memory
from tools.memory.memory_graph import _display_user
from ev.utils import config
from ui.dialogs.memory_list import MemoryListDialog
from ui.pages.plugins_page import _clear_layout
from ui.widgets.role_card import _RoleCard

# 主程序导出的图谱快照（跨进程 ChromaDB HNSW 索引读不到向量，改走文件）
_GRAPH_EXPORT_FILE = os.path.join(
    config.cfg.DATA_ROOT, "memory_graph.json")

# 角色卡片网格列数（对齐预览页 .role-grid: repeat(3, 1fr)）
_ROLE_GRID_COLS = 3
# 角色卡片网格间距（对齐预览页 .plugin-grid: gap 14px）
_ROLE_GRID_SPACING = 14
# 角色卡片摘要最大字符数（避免长记忆撑爆卡片高度，对齐预览 .role-preview 2 行）
_ROLE_PREVIEW_MAX_CHARS = 60


def _load_graph_export():
    """读主程序导出的图谱快照 files；文件缺失/损坏返回 None。"""
    try:
        with open(_GRAPH_EXPORT_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        files = data.get("files")
        if files is None:
            return None
        return files
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def _latest_preview(mems: list) -> str:
    """取最新一条记忆的内容摘要作为角色卡片预览（最多 60 字 + …）。"""
    if not mems:
        return ""
    latest = max(
        mems,
        key=lambda m: m.get("updated_at") or m.get("created_at") or "")
    content = str(latest.get("content") or "").strip().replace("\n", " ")
    if not content:
        return ""
    if len(content) > _ROLE_PREVIEW_MAX_CHARS:
        return content[:_ROLE_PREVIEW_MAX_CHARS] + "…"
    return content


class MemoryPage:
    """记忆页逻辑：角色卡片网格渲染 / 用户点击 / 记忆删除过滤。"""

    def _init_memory_page(self) -> None:
        """记忆页：角色卡片网格视图（替代旧力导向图谱）。

        .ui 里 graph_memory 是占位 QGraphicsView，运行时替换为 QScrollArea
        包裹的角色卡片网格容器；用户分组按 self 优先 + 记忆数倒序排布。
        """
        self.btn_memory_refresh.clicked.connect(self._refresh_memory)
        # 替换 .ui 占位 QGraphicsView 为可滚动的角色卡片网格容器
        placeholder = self.graph_memory
        scroll = QScrollArea(self.ui)
        scroll.setObjectName("role_grid_scroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        inner = QWidget(scroll)
        inner.setObjectName("role_grid_inner")
        self._role_grid_layout = QVBoxLayout(inner)
        self._role_grid_layout.setContentsMargins(0, 0, 0, 0)
        self._role_grid_layout.setSpacing(_ROLE_GRID_SPACING)
        self._role_grid_layout.setAlignment(Qt.AlignTop)
        scroll.setWidget(inner)
        self._role_grid_inner = inner
        lay = placeholder.parentWidget().layout()
        if lay is not None:
            lay.replaceWidget(placeholder, scroll)
        placeholder.deleteLater()
        # 保留 self.graph_memory 引用（兼容其他可能引用，新视图是 scroll）
        self.graph_memory = scroll
        # 状态栏文本可能较长（点击卡片显示摘要）：水平方向 Ignored，
        # 布局不再被其 sizeHint 撑大（文本超宽自动裁切，不撑宽窗口）
        self.label_memory_status.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self._deleted_ids: set = set()  # 本会话已删除的记忆 id（跨进程快照会残留，过滤掉）
        self._refresh_memory()

    def _on_user_clicked(self, user: str) -> None:
        """角色卡片单击：弹出该用户记忆列表弹窗，可逐条查看/删除。"""
        mems = [f for f in self._mem_files
                if (f.get("user") or "chao") == user]
        self.label_memory_status.setText(
            f"用户 {_display_user(user)}：{len(mems)} 条记忆")
        dlg = MemoryListDialog(user, mems, self.ui)
        dlg.deleted.connect(self._on_memory_deleted)  # 删除后过滤并刷新网格
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()

    def _on_memory_deleted(self, node_id: str) -> None:
        """弹窗内删除记忆成功：本地过滤该条并刷新网格。

        数据源是主程序导出的快照——主程序进程内的 ChromaDB 索引不会感知
        控制中心进程的删除（跨进程限制），若只读快照，刷新后已删记忆会
        重新出现。本地记下本会话删除的 id 并过滤，保证 UI 立即消失；
        主程序重启后彻底一致（库里已无该条）。
        """
        self._deleted_ids.add(node_id)
        self._refresh_memory()

    def _refresh_memory(self) -> None:
        """读取全部记忆并重建角色卡片网格。

        数据源优先主程序导出的图谱快照 data/memory_graph.json——控制中心
        与主程序是两个进程，ChromaDB PersistentClient 直连读不到另一进程
        的写入，快照由主程序在记忆变更后原子导出（见
        memory.export_graph_data），读它跨进程稳定；快照缺失才直连存储。
        """
        mm = memory.get_manager()
        snap_files = _load_graph_export()
        files = snap_files
        if files is None:
            try:
                files, _ = mm.graph_data(limit=200)
            except Exception as e:
                self.label_memory_status.setText(f"读取记忆失败：{type(e).__name__}")
                return
        # 过滤本会话已删除的记忆（跨进程快照仍含它们，见 _on_memory_deleted）
        if self._deleted_ids:
            files = [f for f in files
                     if f.get("id") not in self._deleted_ids]
        self._mem_files = files
        self._set_role_memories(files)
        emb = "向量检索" if mm.embedding_enabled else "LLM 回退检索"
        self.label_memory_status.setText(
            f"共 {len(files)} 个记忆文件（{emb}）")

    def _set_role_memories(self, files: list) -> None:
        """按 user 分组重建角色卡片网格（对齐预览页 .role-grid）。

        排序：AI 自身（user="self"）优先，其次按记忆条数倒序，最后按 user
        字母序兜底。每行 3 张 _RoleCard，stretch=1 让 3 张卡片等宽。
        """
        layout = self._role_grid_layout
        _clear_layout(layout)
        # 按 user 分组（user 缺失视为默认 "chao"）
        groups: dict = {}
        for f in files or []:
            u = f.get("user") or "chao"
            groups.setdefault(u, []).append(f)
        if not groups:
            empty = QLabel("暂无记忆", self._role_grid_inner)
            empty.setObjectName("mem_empty")
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(empty)
            return
        # 排序：self 优先 → 记忆数倒序 → user 字母序
        sorted_users = sorted(
            groups.keys(),
            key=lambda u: (u != "self", -len(groups[u]), u))
        for start in range(0, len(sorted_users), _ROLE_GRID_COLS):
            chunk = sorted_users[start:start + _ROLE_GRID_COLS]
            if not chunk:
                break
            row_lay = QHBoxLayout()
            row_lay.setSpacing(_ROLE_GRID_SPACING)
            for user in chunk:
                mems = groups[user]
                card = _RoleCard(user, len(mems), _latest_preview(mems))
                card.clicked.connect(self._on_user_clicked)
                # stretch=1 让每张卡片等宽
                row_lay.addWidget(card, 1)
            # 不足 3 张时尾部补 stretch，避免最后一张被横向撑满
            if len(chunk) < _ROLE_GRID_COLS:
                row_lay.addStretch(1)
            layout.addLayout(row_lay)
        layout.addStretch(1)  # 卡片不足一屏时顶部对齐，不撑满
