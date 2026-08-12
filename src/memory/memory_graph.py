"""记忆图谱控件：把记忆文件按归属者分组渲染为可点击的图谱。

全新实现（不依赖旧版），仅对外保持控制中心需要的三个接口：
- set_memories(files)：注入记忆文件列表后重绘
- 信号 nodeClicked(node) / userClicked(user) / blankClicked()
- _display_user(user)：内部 user 标识 → 显示名

布局：每个归属者一个随机散布的簇（簇心为用户名圆），簇内记忆节点
以簇心为中心环形排布；每次刷新数据都会重新随机分布（散点式布局，
避免固定网格的规整感）。

交互（对齐旧版图谱，全部自绘处理）：
- 单击节点 → nodeClicked（控制中心弹窗看全文）
- 单击簇心（用户名圆）→ userClicked（查看该用户记忆汇总）
- 左键拖动节点 → 移动位置，连边实时跟随
- 左键拖动簇心 → 该 user 整簇节点整体平移（相对位置不变）
- 按住中键拖动 → 平移视图；滚轮 → 围绕光标缩放
"""

from __future__ import annotations

import math
import random
from typing import Optional

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFontMetricsF, QPainter, QPen, QRadialGradient
from PySide6.QtWidgets import QWidget

from src.memory.memory import _USER_DEFAULT, _USER_SELF

# 记忆节点绘制参数（对齐旧版图谱：大圆 + 内容摘要 + 分层配色）
_NODE_RADIUS = 20
_USER_RADIUS = 30

# 随机散点布局参数（世界坐标，_fit 统一适配到视口）
_CANVAS_W = 2000          # 布局画布宽
_CANVAS_H = 1300          # 布局画布高
_CANVAS_MARGIN = 170      # 簇心离画布边缘的留白
_CLUSTER_GAP = 24         # 相邻簇包围圆之间的最小间隔（禁止遮挡）
_RING_BASE = 44           # 节点第一圈相对簇心的半径（用户圆 30 + 间隔）
_RING_STEP = 58           # 每增加一圈的半径增量
_NODE_SPACING = 50        # 同圈相邻节点弧长（直径 40 + 间隔）

# 图层 → 节点主色（与旧版四层语义一致：core 核心 / state 状态 /
# preference 偏好 / archive 摘要；其余按 track 或灰色兜底）
_LAYER_STYLE = {
    "core": QColor(140, 100, 200),        # 紫：核心身份
    "state": QColor(70, 140, 210),        # 蓝：关系状态
    "preference": QColor(230, 130, 60),   # 橙：长期偏好
    "archive": QColor(90, 160, 100),      # 绿：历史摘要
}
_OTHER_COLOR = QColor(150, 150, 150)
# track → 节点主色（skill 技能橙；memory 归入通用蓝，有 layer 前缀时优先）
_TRACK_COLORS = {
    "skill": QColor(230, 130, 60),
    "memory": QColor(70, 140, 210),
}
_USER_COLOR = QColor(88, 170, 168)        # 用户簇心：暖青（旧版同款）
_EDGE_COLOR = QColor(120, 120, 120)       # 用户连边：灰色

# 视图缩放范围（滚轮）
_MIN_SCALE = 0.2
_MAX_SCALE = 8.0


def _content_summary(content: str, max_chars: int = 6) -> str:
    """节点标签：记忆内容摘要（去换行，超长省略）。

    节点直径 40px 仅够显示约 5-6 个中文字；完整内容由
    hover tooltip 与点击弹窗提供。
    """
    t = (content or "").strip().replace("\n", " ").replace("\r", " ")
    t = t.strip()
    if not t:
        return "?"
    if len(t) > max_chars:
        return t[:max_chars] + "…"
    return t


def _node_color(item: dict) -> QColor:
    """节点主色：description 的 layer 前缀（core/state/preference/archive）优先，
    其次 track，最后灰色兜底。"""
    desc = (item.get("description") or "").strip()
    layer = desc.split("/")[0].strip().lower()
    if layer in _LAYER_STYLE:
        return _LAYER_STYLE[layer]
    return _TRACK_COLORS.get(item.get("track") or "memory", _OTHER_COLOR)


def _display_user(user: str) -> str:
    """把内部 user 标识映射为 UI 显示名。"""
    mapping = {_USER_SELF: "我自己", _USER_DEFAULT: "你"}
    return mapping.get(str(user), str(user))


class MemoryGraphWidget(QWidget):
    """记忆图谱控件：用户簇 + 记忆节点，支持点击与拖动交互。"""

    nodeClicked = Signal(object)
    userClicked = Signal(str)
    blankClicked = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._memories: list[dict] = []
        # 是否显示记忆节点：False 时图谱只显示用户簇心（人名），
        # 点击人名由控制中心打开该用户记忆列表管理
        self.show_memory_nodes = True
        # 布局缓存：世界坐标（布局结果），绘制/命中经 _to_screen 转换
        self._clusters: list[tuple[QPointF, str, list[tuple[QPointF, dict]]]] = []
        # 视图变换（平移/缩放）
        self._scale = 1.0
        self._offset = QPointF(0.0, 0.0)
        # 拖动状态
        self._click_node: Optional[tuple[int, int]] = None   # 按下命中的节点 (簇, 节点)
        self._drag_node: Optional[tuple[int, int]] = None    # 正在拖动的节点
        self._click_user: Optional[str] = None               # 按下命中的簇心
        self._drag_user: Optional[str] = None                # 正在拖动的簇心
        self._drag_user_ci: int = -1
        self._drag_user_pos0: QPointF = QPointF()
        self._drag_user_nodes0: list[QPointF] = []
        self._drag_start_w: QPointF = QPointF()              # 按下时鼠标世界坐标
        self._drag_moved = False
        self._panning = False
        self._pan_start: QPointF = QPointF()
        self._pan_offset0: QPointF = QPointF()
        self.setMouseTracking(True)

    # ---------- 数据 ----------

    def set_memories(self, files: list[dict]) -> None:
        """注入记忆文件列表并重绘（布局后整体适配控件尺寸）。"""
        self._memories = list(files or [])
        self._layout_clusters()
        self._fit()

    # ---------- 坐标变换 ----------

    def _to_screen(self, world: QPointF) -> QPointF:
        return QPointF(world.x() * self._scale + self._offset.x(),
                       world.y() * self._scale + self._offset.y())

    def _to_world(self, screen: QPointF) -> QPointF:
        return QPointF((screen.x() - self._offset.x()) / self._scale,
                       (screen.y() - self._offset.y()) / self._scale)

    def _fit(self) -> None:
        """把整图适配到控件尺寸（居中），节点保底可点。"""
        if not self._memories:
            return
        xs: list[float] = []
        ys: list[float] = []
        for user_pos, owner, nodes in self._clusters:
            xs.append(user_pos.x())
            ys.append(user_pos.y())
            for node_pos, item in nodes:
                xs.append(node_pos.x())
                ys.append(node_pos.y())
        minx, maxx = min(xs), max(xs)
        miny, maxy = min(ys), max(ys)
        bw = max(maxx - minx, 10.0)
        bh = max(maxy - miny, 10.0)
        margin = 40.0
        sw = max(self.width() - 2 * margin, 50.0)
        sh = max(self.height() - 2 * margin, 50.0)
        self._scale = min(sw / bw, sh / bh, 3.0)
        cx = (minx + maxx) / 2
        cy = (miny + maxy) / 2
        self._offset = QPointF(self.width() / 2 - cx * self._scale,
                               self.height() / 2 - cy * self._scale)
        self.update()

    # ---------- 布局 ----------

    def _cluster_radius(self, count: int) -> float:
        """簇的包围圆半径：最外圈节点到簇心的距离 + 节点半径；
        不显示记忆节点时退化为簇心半径。"""
        if not self.show_memory_nodes:
            return _USER_RADIUS
        per_ring = max(4, int(2 * math.pi * _RING_BASE / _NODE_SPACING))
        rings = max(0, (count - 1) // per_ring)
        return _RING_BASE + rings * _RING_STEP + _NODE_RADIUS

    def _place_cluster(self, placed: list[tuple[float, float, float]],
                       radius: float, rng: random.Random) -> tuple[float, float]:
        """为簇找不遮挡的位置：随机候选 + 网格扫描兜底（保证不重叠）。

        placed：[(cx, cy, 包围半径), ...]。约束为两个簇的包围圆间距
        ≥ _CLUSTER_GAP。随机尝试失败后按步长扫描画布，总能找到位置。
        """
        def fits(x: float, y: float) -> bool:
            for px, py, pr in placed:
                need = radius + pr + _CLUSTER_GAP
                if (x - px) ** 2 + (y - py) ** 2 < need * need:
                    return False
            return True

        for _ in range(300):
            x = _CANVAS_MARGIN + rng.random() * (_CANVAS_W - 2 * _CANVAS_MARGIN)
            y = _CANVAS_MARGIN + rng.random() * (_CANVAS_H - 2 * _CANVAS_MARGIN)
            if fits(x, y):
                return x, y
        # 随机全失败（簇很多时）：网格扫描兜底，仍保证不重叠
        step = 36.0
        y = _CANVAS_MARGIN
        while y < _CANVAS_H - _CANVAS_MARGIN:
            x = _CANVAS_MARGIN
            while x < _CANVAS_W - _CANVAS_MARGIN:
                if fits(x, y):
                    return x, y
                x += step
            y += step
        # 理论不可达（画布不足以容纳全部簇）：取离所有簇最远的点，尽力避免遮挡
        best_x, best_y, best_d = _CANVAS_MARGIN, _CANVAS_MARGIN, -1.0
        y = _CANVAS_MARGIN
        while y < _CANVAS_H - _CANVAS_MARGIN:
            x = _CANVAS_MARGIN
            while x < _CANVAS_W - _CANVAS_MARGIN:
                d = min((x - px) ** 2 + (y - py) ** 2
                        for px, py, pr in placed)
                if d > best_d:
                    best_d, best_x, best_y = d, x, y
                x += step
            y += step
        return best_x, best_y

    def _layout_clusters(self) -> list[tuple[QPointF, str, list[tuple[QPointF, dict]]]]:
        """随机散点布局：AI 自己的记忆簇固定画布正中，其余簇环绕散布。

        簇间通过包围圆间距约束严格不重叠（_place_cluster），簇内节点
        绕簇心环形排布。坐标为世界坐标，_fit 统一适配到视口。
        """
        groups: dict[str, list[dict]] = {}
        for item in self._memories:
            owner = str(item.get("user") or _USER_DEFAULT)
            groups.setdefault(owner, []).append(item)

        # AI 自己（self）的簇第一个放置，固定在画布中心
        self_items = groups.pop(_USER_SELF, [])
        ordered: list[tuple[str, list[dict]]] = []
        if self_items:
            ordered.append((_USER_SELF, self_items))
        ordered.extend((owner, items) for owner, items in groups.items())

        rng = random.Random()  # 无种子：每次布局都随机散布
        placed: list[tuple[float, float, float]] = []  # (cx, cy, 包围半径)
        clusters: list[tuple[QPointF, str, list[tuple[QPointF, dict]]]] = []
        for owner, items in ordered:
            if owner == _USER_SELF:
                cx, cy = _CANVAS_W / 2, _CANVAS_H / 2
            else:
                radius = self._cluster_radius(len(items))
                cx, cy = self._place_cluster(placed, radius, rng)
            placed.append((cx, cy, self._cluster_radius(len(items))))
            user_pos = QPointF(cx, cy)

            # 簇内节点：以簇心为中心的同心圆环形排布（多圈扩展 + 轻微抖动）；
            # 不显示节点时簇内为空，只保留簇心
            node_positions: list[tuple[QPointF, dict]] = []
            if self.show_memory_nodes:
                per_ring = max(4, int(2 * math.pi * _RING_BASE / _NODE_SPACING))
                for index, item in enumerate(items):
                    ring = index // per_ring
                    angle = (2 * math.pi * (index % per_ring) / per_ring
                             + rng.uniform(-0.15, 0.15))
                    radius = _RING_BASE + ring * _RING_STEP + rng.uniform(-6.0, 6.0)
                    node_positions.append((
                        QPointF(cx + math.cos(angle) * radius,
                                cy + math.sin(angle) * radius),
                        item,
                    ))
            clusters.append((user_pos, owner, node_positions))
        self._clusters = clusters
        return clusters

    # ---------- 命中检测 ----------

    def _hit_node(self, screen: QPointF) -> tuple[int, int]:
        """屏幕坐标命中检测：返回 (簇索引, 节点索引)；未命中返回 (-1, -1)。
        不显示记忆节点时恒不命中。"""
        if not self.show_memory_nodes:
            return -1, -1
        for ci, (user_pos, owner, nodes) in enumerate(self._clusters):
            for ni, (node_pos, item) in enumerate(nodes):
                c = self._to_screen(node_pos)
                if (screen.x() - c.x()) ** 2 + (screen.y() - c.y()) ** 2 \
                        <= (_NODE_RADIUS + 4) ** 2:
                    return ci, ni
        return -1, -1

    def _hit_user(self, screen: QPointF) -> Optional[str]:
        """屏幕坐标命中检测：返回命中的 user 标识；未命中返回 None。"""
        for user_pos, owner, nodes in self._clusters:
            c = self._to_screen(user_pos)
            if (screen.x() - c.x()) ** 2 + (screen.y() - c.y()) ** 2 \
                    <= (_USER_RADIUS + 4) ** 2:
                return owner
        return None

    # ---------- 绘制 ----------

    def paintEvent(self, event) -> None:
        """绘制背景、用户连边、用户簇心与记忆节点。"""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        # 背景：暖白底 + 中心放射柔光（对齐旧版图谱的视觉风格）
        painter.fillRect(self.rect(), QColor(250, 249, 245))
        center = QPointF(self.width() / 2, self.height() / 2)
        radius = max(self.width(), self.height()) * 0.6
        grad = QRadialGradient(center, radius)
        grad.setColorAt(0.0, QColor(140, 100, 200, 50))
        grad.setColorAt(0.35, QColor(90, 140, 210, 18))
        grad.setColorAt(1.0, QColor(0, 0, 0, 0))
        painter.fillRect(self.rect(), grad)

        clusters = self._clusters

        # 用户连边：簇心 → 该 user 每个记忆（灰色实线）；不显示节点时跳过
        if self.show_memory_nodes:
            edge = QColor(_EDGE_COLOR)
            edge.setAlpha(100)
            painter.setPen(QPen(edge, 1.6))
            for user_pos, owner, nodes in clusters:
                for node_pos, item in nodes:
                    painter.drawLine(self._to_screen(user_pos),
                                     self._to_screen(node_pos))

        for user_pos, owner, nodes in clusters:
            # 用户簇心：暖青色半透明圆 + 同色描边，白字显示归属名
            uc = self._to_screen(user_pos)
            painter.save()
            fill = QColor(_USER_COLOR)
            fill.setAlpha(70)
            painter.setBrush(fill)
            painter.setPen(QPen(_USER_COLOR, 1.6))
            painter.drawEllipse(uc, _USER_RADIUS, _USER_RADIUS)
            font = painter.font()
            font.setBold(True)
            font.setPixelSize(13)
            painter.setFont(font)
            fm = QFontMetricsF(font)
            label = _display_user(owner)
            if fm.horizontalAdvance(label) > _USER_RADIUS * 2 - 12:
                while label and fm.horizontalAdvance(label + "…") > _USER_RADIUS * 2 - 12:
                    label = label[:-1]
                label += "…"
            painter.setPen(QColor(255, 255, 255))
            painter.drawText(
                QRectF(uc.x() - _USER_RADIUS, uc.y() - _USER_RADIUS,
                       _USER_RADIUS * 2, _USER_RADIUS * 2),
                Qt.AlignCenter, label,
            )
            painter.restore()
            # 记忆节点：分层色半透明圆 + 同色描边，白字显示内容摘要（截断）；
            # 不显示节点时跳过
            if not self.show_memory_nodes:
                continue
            fm = QFontMetricsF(painter.font())
            for node_pos, item in nodes:
                nc = self._to_screen(node_pos)
                color = _node_color(item)
                fill = QColor(color)
                fill.setAlpha(60)
                painter.setBrush(fill)
                painter.setPen(QPen(color, 1.6))
                painter.drawEllipse(nc, _NODE_RADIUS, _NODE_RADIUS)
                label = _content_summary(item.get("content") or "")
                if fm.horizontalAdvance(label) > _NODE_RADIUS * 2 - 10:
                    while label and fm.horizontalAdvance(label + "…") > _NODE_RADIUS * 2 - 10:
                        label = label[:-1]
                    label += "…"
                painter.setPen(QColor(255, 255, 255))
                painter.drawText(
                    QRectF(nc.x() - _NODE_RADIUS, nc.y() - _NODE_RADIUS,
                           _NODE_RADIUS * 2, _NODE_RADIUS * 2),
                    Qt.AlignCenter, label,
                )
        painter.end()

    # ---------- 交互（对齐旧版：拖动节点/簇心、中键平移、滚轮缩放） ----------

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MiddleButton:
            # 按住中键拖动 → 平移视图（不移动节点）
            self._panning = True
            self._pan_start = event.position()
            self._pan_offset0 = self._offset
            self.update()
        elif event.button() == Qt.LeftButton:
            pos = event.position()
            ci, ni = self._hit_node(pos)
            if ni >= 0:
                # 按下节点：未拖动松开 → 单击弹窗；拖动 → 移动节点
                self._click_node = (ci, ni)
                self._drag_node = (ci, ni)
                self._drag_moved = False
            else:
                owner = self._hit_user(pos)
                if owner is not None:
                    # 按下簇心：未拖动松开 → userClicked；拖动 → 整簇平移
                    self._click_user = owner
                    self._drag_user = owner
                    self._drag_moved = False
                    self._drag_start_w = self._to_world(pos)
                    for uci, (up, o, ns) in enumerate(self._clusters):
                        if o == owner:
                            self._drag_user_ci = uci
                            self._drag_user_pos0 = up
                            self._drag_user_nodes0 = [p for p, it in ns]
                            break
                else:
                    self.blankClicked.emit()
            self.update()
        event.accept()

    def mouseMoveEvent(self, event) -> None:
        pos = event.position()
        if self._drag_node is not None:
            # 拖动节点：世界坐标跟随鼠标，连边随重绘更新
            ci, ni = self._drag_node
            nodes = self._clusters[ci][2]
            nodes[ni] = (self._to_world(pos), nodes[ni][1])
            self._drag_moved = True
            self.update()
        elif self._drag_user is not None:
            # 拖动簇心：该 user 的整簇节点整体平移（相对位置不变）
            delta = self._to_world(pos) - self._drag_start_w
            ci = self._drag_user_ci
            up, o, nodes = self._clusters[ci]
            self._clusters[ci] = (
                self._drag_user_pos0 + delta,
                o,
                [(p0 + delta, nodes[i][1])
                 for i, p0 in enumerate(self._drag_user_nodes0)],
            )
            self._drag_moved = True
            self.update()
        elif self._panning:
            d = pos - self._pan_start
            self._offset = QPointF(self._pan_offset0.x() + d.x(),
                                   self._pan_offset0.y() + d.y())
            self.update()
        event.accept()

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            if self._click_node is not None:
                # 单击（按下后未拖动）→ 通知控制中心弹窗看内容
                if not self._drag_moved:
                    ci, ni = self._click_node
                    self.nodeClicked.emit(self._clusters[ci][2][ni][1])
                self._click_node = None
            self._drag_node = None
            if self._click_user is not None:
                # 簇心：未拖动松开 → 单击查看记忆汇总；已拖动 → 仅落位
                if not self._drag_moved:
                    self.userClicked.emit(self._click_user)
                self._click_user = None
            self._drag_user = None
        if event.button() == Qt.MiddleButton:
            self._panning = False
        event.accept()

    def wheelEvent(self, event) -> None:
        factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
        new_scale = min(max(self._scale * factor, _MIN_SCALE), _MAX_SCALE)
        anchor = event.position()
        world_anchor = self._to_world(anchor)
        self._scale = new_scale
        # 保持鼠标下的世界点不动（围绕光标缩放）
        self._offset = QPointF(anchor.x() - world_anchor.x() * new_scale,
                               anchor.y() - world_anchor.y() * new_scale)
        self.update()
        event.accept()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        # 首次显示（隐藏 tab 变大）或窗口缩放时重新适配
        if self._memories and not self._panning \
                and self._drag_node is None and self._drag_user is None:
            self._fit()


__all__ = ["MemoryGraphWidget", "_display_user"]
