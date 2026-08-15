"""记忆页（mixin）：图谱渲染 / 记忆详情与列表弹窗 / 删除刷新。"""

import json
import os
import time

from PySide6.QtWidgets import QSizePolicy

from src.memory import memory
from src.memory.memory_graph import MemoryGraphWidget, _display_user
from src.utils import config
from ui.dialogs.memory_detail import MemoryDetailDialog
from ui.dialogs.memory_list import MemoryListDialog

# 主程序导出的图谱快照（跨进程 ChromaDB HNSW 索引读不到向量，改走文件）
_GRAPH_EXPORT_FILE = os.path.join(
    config.cfg.PROJECT_ROOT, "data", "memory_graph.json")


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


class MemoryPage:
    """记忆页逻辑：图谱自绘替换、节点/用户点击、记忆删除过滤。"""

    def _init_memory_page(self) -> None:
        """记忆页：仅图谱视图（表格视图已移除）。

        图谱替换 .ui 里的占位 QGraphicsView 为自绘用户分簇放射星状图；
        表格/图谱切换控件（stack_memory 的表格页、切换按钮）不再使用，
        启动直接落在图谱页。
        """
        self.btn_memory_refresh.clicked.connect(self._refresh_memory)
        # 图谱：.ui 里 graph_memory 是占位 QGraphicsView，替换为自绘力导向网状图谱
        placeholder = self.graph_memory
        self.graph_memory = MemoryGraphWidget(self.ui)
        lay = placeholder.parentWidget().layout()
        if lay is not None:
            lay.replaceWidget(placeholder, self.graph_memory)
        placeholder.deleteLater()
        self.graph_memory.nodeClicked.connect(self._on_graph_node_clicked)
        self.graph_memory.userClicked.connect(self._on_user_clicked)
        self.graph_memory.blankClicked.connect(self._on_graph_blank_clicked)
        # 只显示人名（用户簇心），不显示每条记忆圆点：点击人名弹记忆列表管理
        self.graph_memory.show_memory_nodes = False
        # 状态栏文本可能较长（点击节点显示摘要）：水平方向 Ignored，
        # 布局不再被其 sizeHint 撑大（文本超宽自动裁切，不撑宽窗口）
        self.label_memory_status.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        # 表格视图已移除：隐藏切换按钮、直接显示图谱页
        self.btn_memory_view_toggle.setVisible(False)
        self.stack_memory.setCurrentIndex(1)
        self._deleted_ids: set = set()  # 本会话已删除的记忆 id（跨进程快照会残留，过滤掉）
        self._refresh_memory()

    def _on_graph_blank_clicked(self) -> None:
        """图谱空白区域点击：若记忆详情弹窗开着则关闭，否则提示操作。"""
        dlg = getattr(self, "_mem_dlg", None)
        if dlg is not None and dlg.isVisible():
            dlg._fade_out_and_close()  # 淡出关闭当前弹窗
            return
        self.label_memory_status.setText(
            "点击用户名圆查看该用户记忆并管理（删除）")

    def _on_user_clicked(self, user: str) -> None:
        """图谱簇心（用户名圆）单击：弹出该用户记忆列表弹窗，可逐条查看/删除。"""
        mems = [f for f in self._mem_files
                if (f.get("user") or "chao") == user]
        self.label_memory_status.setText(
            f"用户 {_display_user(user)}：{len(mems)} 条记忆")
        dlg = MemoryListDialog(user, mems, self.ui)
        dlg.deleted.connect(self._on_memory_deleted)  # 删除后过滤并刷新图谱
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()

    def _on_graph_node_clicked(self, node: dict) -> None:
        """图谱节点单击：状态栏显示摘要 + 弹出该记忆详情弹窗。

        弹窗加防抖（同一节点 0.6s 内不重复弹），避免双击被当作
        两次单击而连续弹出两次。
        """
        now = time.monotonic()
        name = str(node.get("name") or "")
        content = str(node.get("content") or "")
        last = getattr(self, "_last_popup", None)
        if last is not None and last[0] == name and now - last[1] < 0.6:
            return  # 双击的第二次单击：不重复弹
        self._last_popup = (name, now)
        body = content.strip().replace("\n", " ")
        if len(body) > 80:
            body = body[:80] + "…"
        self.label_memory_status.setText(f"已选中：{name} — {body}")
        self._show_memory_detail(node)

    def _show_memory_detail(self, node: dict) -> None:
        """点击记忆节点/条目：弹出记忆详情卡片（非模态，淡入动画）。

        非模态 + 实例跟踪：点击图谱空白区域（blankClicked）可关闭当前
        弹窗；再次点击其他记忆会先关旧的再开新的。
        """
        if getattr(self, "_mem_dlg", None) is not None:
            self._mem_dlg.close()  # 先关旧弹窗（可能有淡出动画未走完）
        dlg = MemoryDetailDialog(node, self.ui)
        dlg.deleted.connect(self._on_memory_deleted)  # 弹窗内删除成功后刷新
        dlg.finished.connect(self._on_mem_dlg_finished)
        self._mem_dlg = dlg
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()

    def _on_mem_dlg_finished(self, *_args) -> None:
        """弹窗关闭后清空跟踪引用（避免悬空/重复关闭）。"""
        self._mem_dlg = None

    def _on_memory_deleted(self, node_id: str) -> None:
        """弹窗内删除记忆成功：本地过滤该条并刷新。

        数据源是主程序导出的快照——主程序进程内的 ChromaDB 索引不会感知
        控制中心进程的删除（跨进程限制），若只读快照，刷新后已删记忆会
        重新出现。本地记下本会话删除的 id 并过滤，保证 UI 立即消失；
        主程序重启后彻底一致（库里已无该条）。
        """
        self._deleted_ids.add(node_id)
        self._refresh_memory()

    def _refresh_memory(self) -> None:
        """读取全部记忆并重建图谱。

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
        self.graph_memory.set_memories(files)
        emb = "向量检索" if mm.embedding_enabled else "LLM 回退检索"
        self.label_memory_status.setText(
            f"共 {len(files)} 个记忆文件（{emb}）")
