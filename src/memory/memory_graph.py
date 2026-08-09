"""记忆图谱渲染：纯 QWidget 自绘**用户分簇放射星状图**（交互完全自控，绝对可靠）。

结构（星状，每个 user 一个放射星）：
  节点 = 记忆文件（按层着色：core 核心身份 / state 关系状态 /
                preference 长期偏好 / archive 历史摘要 / 其他）。
  簇   = 同一 user（归属人）的记忆聚成一簇；簇心显示用户名标签。
  边   = 用户连边（灰色实线）：从簇心（用户名）引到该 user 的每个记忆。
  布局 = 多中心放射布局：每个 user 一个放射星（星心 = 用户名，该 user 的
        记忆围绕星心分圈放射）；多个放射星围绕全局圆心环形排列，各自独立
        成簇；节点可拖动微调。

交互（全部自绘处理，不依赖 QGraphicsScene 事件系统）：
- 单击节点 → 弹窗查看记忆全文（nodeClicked 信号，由控制中心弹窗）
- 单击簇心（用户名圆）→ 弹窗查看该 user 的记忆汇总（userClicked 信号）
- 左键拖动节点 → 移动位置，连线实时跟随
- 左键拖动簇心（用户名圆）→ 该 user 的整颗放射星整体平移（相对位置不变）
- 按住中键拖动 → 平移视图；滚轮 → 围绕鼠标缩放
- 悬停节点 → 高亮 + 手型光标 + tooltip（文件=全文）
- 节点为固定屏幕半径圆（永远可点，缩放只改变间距不变小）
"""

import math
from typing import Dict, List, Optional, Tuple

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QPainter, QPen, QRadialGradient
from PySide6.QtWidgets import QWidget

# 图层 → 节点主色（与记忆页四层语义一致）
_LAYER_STYLE = {
    "core": QColor(140, 100, 200),        # 紫：核心身份
    "state": QColor(70, 140, 210),        # 蓝：关系状态
    "preference": QColor(230, 130, 60),   # 橙：长期偏好
    "archive": QColor(90, 160, 100),      # 绿：历史摘要
}
_OTHER_COLOR = QColor(150, 150, 150)
# AI 自我（user="self"）在 UI 的显示名：本项目的 AI 是 Neuro（neuro-sama）
_SELF_DISPLAY_NAME = "neuro"


def _display_user(user: str) -> str:
    """归属显示名：self（AI 自我）→ AI 名字，其余原样返回。"""
    return _SELF_DISPLAY_NAME if user == "self" else (user or "")
# 图层节点显示名（description 前缀 → 中文标签，悬停 tooltip 用）
_LAYER_NAMES = {
    "core": "核心身份",
    "state": "关系状态",
    "preference": "长期偏好",
    "archive": "历史摘要",
}

# 记忆归属者缺省（与 src/memory/memory.py _USER_DEFAULT 一致）
_USER_DEFAULT = "chao"


def _content_summary(content: str, max_chars: int = 6) -> str:
    """节点标签：记忆内容摘要（去换行，超长省略）。

    节点直径 44px 仅够显示约 5-6 个中文字；完整内容由
    hover tooltip 与点击弹窗提供。
    """
    t = (content or "").strip().replace("\n", " ").replace("\r", " ")
    t = t.strip()
    if not t:
        return "?"
    if len(t) > max_chars:
        return t[:max_chars] + "…"
    return t


def _tip_html(title: str, body: str) -> str:
    """富文本 tooltip：加粗标题 + 换行正文（配合全局 QToolTip QSS 呈现暖色卡片）。

    与 ui/control_center.py 的 _tooltip_html 同实现；本地复制避免循环导入。
    """
    import html
    safe_title = html.escape(str(title or "")).strip()
    safe_body = html.escape(str(body or "")).replace("\n", "<br>")
    style = "white-space:pre-wrap; max-width:420px"
    if safe_title:
        return (f"<div style='{style}'><b>{safe_title}</b>"
                f"<br><br>{safe_body}</div>")
    return f"<div style='{style}'>{safe_body}</div>"


def _radial_layout(nodes: List[dict],
                   groups: Optional[Dict[int, str]] = None
                   ) -> Tuple[List[QPointF], Dict[str, QPointF]]:
    """多中心放射布局（每个 user 一个「放射星」）：

      星心 = 用户中心（不占用节点——簇心绘制用户名标签）；
      该 user 的记忆围绕星心分圈放射（内圈 → 外圈，每圈 8 个）；
      多个放射星围绕全局圆心环形排列，各自独立成簇。
      用户连边（虚线）即从星心引到每个成员。

    groups = {节点索引: user}；未指定时全部归同一组（单星）。
    返回 (节点位置, {user: 星心位置})。布局后归一化到固定画布范围，
    保证 _fit 缩放后节点不重叠。
    """
    n = len(nodes)
    if n == 0:
        return [], {}
    if groups is None:
        groups = {}
    group_members: dict = {}
    for i in range(n):
        group_members.setdefault(groups.get(i, ""), []).append(i)
    # 簇排序：大小降序（大 user 靠前），同大小按用户名
    keys = sorted(group_members, key=lambda c: (-len(group_members[c]), c))
    m = len(keys)
    # 星半径 = 成员分圈数决定（每圈 8 个，内圈 260，圈距 205——约等于内圈
    # 弧距 2π·260/8≈204，保证星内成员之间留足间隙、画面不挤）
    star_r = {}
    for key in keys:
        rings = max(1, math.ceil(len(group_members[key]) / 8))
        star_r[key] = 260.0 + (rings - 1) * 205.0
    # 星心环形排列半径：按「m 个等径星刚好不重叠」的最紧圆环排布
    # （相邻星圆心距 = 2R1·sin(π/m) ≥ 星直径 → R1 = 星半径/sin(π/m)）。
    # 比固定倍数更紧凑：星靠得越近、整体包围盒越小，归一化后成员间距越大。
    # m=1 时单星直接居中（R1=0）。
    mstar = max(star_r.values(), default=260.0)
    R1 = mstar / math.sin(math.pi / m) + 30.0 if m >= 2 else 0.0
    out = [QPointF(0.0, 0.0)] * n
    centers: dict = {}
    for gi, key in enumerate(keys):
        ang0 = gi * 2 * math.pi / m - math.pi / 2
        center_pos = QPointF(R1 * math.cos(ang0), R1 * math.sin(ang0))
        centers[key] = center_pos  # 星心 = 用户中心（簇心显示用户名）
        # 成员围绕星心分圈放射（每圈角度均匀，圈间错开避免径向重叠）
        members = group_members[key]
        ring = 0
        j = 0
        remaining = len(members)
        while remaining > 0:
            per = min(remaining, 8)
            for _ in range(per):
                ang = j * 2 * math.pi / per + ring * 0.35
                r = 260.0 + ring * 205.0
                out[members[j]] = center_pos + QPointF(
                    r * math.cos(ang), r * math.sin(ang))
                j += 1
            remaining -= per
            ring += 1
    # 归一化到目标画布范围（保持相对形状）：_fit 缩放后节点不重叠。
    # 星心一并纳入包围盒，避免靠外的用户标签被裁切。
    xs = [p.x() for p in out] + [c.x() for c in centers.values()]
    ys = [p.y() for p in out] + [c.y() for c in centers.values()]
    bw = (max(xs) - min(xs)) or 1.0
    bh = (max(ys) - min(ys)) or 1.0
    s = min(950.0 / bw, 850.0 / bh)
    cx = (min(xs) + max(xs)) / 2.0
    cy = (min(ys) + max(ys)) / 2.0
    return ([QPointF((p.x() - cx) * s, (p.y() - cy) * s) for p in out],
            {k: QPointF((c.x() - cx) * s, (c.y() - cy) * s)
             for k, c in centers.items()})


class MemoryGraphWidget(QWidget):
    """记忆图谱控件：set_memories(list_files() 结果) 后渲染用户分簇放射星状图谱。"""

    nodeClicked = Signal(object)     # 整条记忆 dict（name/description/content/…）
    userClicked = Signal(str)        # 簇心（用户名）点击：user 名
    blankClicked = Signal()          # 点击空白/边（提示用户点圆）

    _NODE_R = 20.0            # 节点屏幕半径（px，固定——永远可点）
    _USER_NODE_R = 30.0       # 用户簇心圆半径（px，比记忆节点大一圈作区别）
    _EDGE_COLOR = QColor(120, 120, 120)      # 连边：灰色实线
    _USER_COLOR = QColor(88, 170, 168)       # 用户簇心：暖青色
    _BG = QColor(250, 249, 245)
    _EMPTY_COLOR = QColor(150, 145, 135)
    _MIN_SCALE = 0.2
    _MAX_SCALE = 8.0

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("graph_memory")
        # 允许 QSS 绘制边框/圆角（背景仍由 paintEvent 自绘）
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setMouseTracking(True)
        self.setMinimumHeight(60)
        self._nodes: List[dict] = []          # {name,desc,content,color,pos,terms}
        self._user_groups: Dict[str, List[int]] = {}   # {user: [节点索引,...]}
        self._user_centers: Dict[str, QPointF] = {}    # {user: 星心（簇心）位置}
        self._scale = 1.0
        self._offset = QPointF(0.0, 0.0)
        self._hover = -1
        self._selected = -1
        self._click_idx = -1       # 按下命中的节点（松开未拖动 → 弹窗）
        self._drag_idx = -1        # 正在拖动的节点
        self._drag_moved = False
        self._panning = False
        self._pan_start = QPointF()
        self._pan_offset0 = QPointF()
        self._empty = False
        self._hover_user = None      # 悬停的簇心（user 名）
        self._selected_user = None   # 选中的簇心
        self._click_user = None      # 按下命中的簇心（松开未拖动 → userClicked）
        self._drag_user = None       # 正在拖动的簇心（整星平移）
        self._drag_start_w = QPointF()      # 按下时鼠标世界坐标
        self._drag_user_center0 = QPointF()  # 簇心原始位置
        self._drag_user_nodes0: dict = {}    # 该 user 节点原始位置 {i: pos}

    # ------------------------------------------------------------------
    # 数据入口
    # ------------------------------------------------------------------

    def set_memories(self, files: Optional[list] = None) -> None:
        """用记忆文件列表重建用户分簇放射星状图谱（files 为 memory.list_files() 结果）。

        同一 user（归属人）的记忆聚成一簇，簇心显示用户名；灰色实线
        从簇心引到该 user 的每个记忆（用户连边）；记忆之间不连边。
        """
        self._nodes = []
        self._user_groups = {}
        self._user_centers = {}
        self._hover = -1
        self._selected = -1
        self._click_idx = -1
        self._drag_idx = -1
        self._drag_moved = False
        self._panning = False
        self._hover_user = None
        self._selected_user = None
        self._click_user = None
        self._drag_user = None
        self._empty = not files
        if not files:
            self.update()
            return
        # 1) 节点
        for f in files:
            desc = f.get("description") or ""
            layer = desc.split("/")[0].strip().lower()
            content = f.get("content") or ""
            self._nodes.append({
                "id": f.get("id") or "",
                "name": f.get("name") or "?",
                # 节点上直接显示记忆内容摘要（metadata 的 name 是哈希名如
                # fact-xxxx，对用户无意义；点击弹窗/hover 仍可看全文）
                "label": _content_summary(content),
                "desc": desc,
                "content": content,
                # 弹窗展示所需的完整元信息（user/track/时间戳，缺失时弹窗兜底）
                "user": f.get("user") or "",
                "track": f.get("track") or "",
                "updated_at": f.get("updated_at") or "",
                "created_at": f.get("created_at") or "",
                "color": _LAYER_STYLE.get(layer, _OTHER_COLOR),
                "tip": _tip_html(f.get("name") or "", content),
                "pos": QPointF(0.0, 0.0),
            })
        # 2) 按 user 分簇：每个 user 一个「放射星」，簇心显示用户名
        self._user_groups = {}
        for i, nd in enumerate(self._nodes):
            self._user_groups.setdefault(
                nd.get("user") or _USER_DEFAULT, []).append(i)
        user_of = {i: u for u, idxs in self._user_groups.items() for i in idxs}
        # 3) 多中心放射布局：每个 user 簇一个放射星，星心 = 用户中心
        #    （不占用节点——簇心绘制用户名标签），该 user 的记忆围绕星心
        #    分圈放射；用户连边（实线）从星心引到每个成员
        positions, user_centers = _radial_layout(self._nodes, user_of)
        self._user_centers = user_centers
        for node, pos in zip(self._nodes, positions):
            node["pos"] = pos
        self._fit()

    # ------------------------------------------------------------------
    # 坐标映射
    # ------------------------------------------------------------------

    def _to_screen(self, w: QPointF) -> QPointF:
        return QPointF(w.x() * self._scale + self._offset.x(),
                       w.y() * self._scale + self._offset.y())

    def _to_world(self, p: QPointF) -> QPointF:
        return QPointF((p.x() - self._offset.x()) / self._scale,
                       (p.y() - self._offset.y()) / self._scale)

    def _hit_node(self, p: QPointF) -> int:
        """屏幕坐标命中检测：点到节点圆心距离 ≤ 半径+4 即命中。"""
        for i, nd in enumerate(self._nodes):
            c = self._to_screen(nd["pos"])
            if (p.x() - c.x()) ** 2 + (p.y() - c.y()) ** 2 \
                    <= (self._NODE_R + 4) ** 2:
                return i
        return -1

    def _hit_user(self, p: QPointF) -> Optional[str]:
        """屏幕坐标命中检测：点到簇心（用户圆）圆心距离 ≤ 半径+4 即命中。"""
        for user, center in self._user_centers.items():
            c = self._to_screen(center)
            if (p.x() - c.x()) ** 2 + (p.y() - c.y()) ** 2 \
                    <= (self._USER_NODE_R + 4) ** 2:
                return user
        return None

    def _fit(self) -> None:
        """把整图适配到控件尺寸（居中），节点保底可点。"""
        if not self._nodes:
            return
        xs = [nd["pos"].x() for nd in self._nodes]
        ys = [nd["pos"].y() for nd in self._nodes]
        # 用户簇心（用户名标签）一并纳入边界，避免靠外的簇心被裁切
        for c in self._user_centers.values():
            xs.append(c.x())
            ys.append(c.y())
        minx, maxx = min(xs), max(xs)
        miny, maxy = min(ys), max(ys)
        bw = max(maxx - minx, 10.0)
        bh = max(maxy - miny, 10.0)
        m = 40.0
        sw = max(self.width() - 2 * m, 50.0)
        sh = max(self.height() - 2 * m, 50.0)
        self._scale = min(sw / bw, sh / bh, 3.0)
        cx = (minx + maxx) / 2
        cy = (miny + maxy) / 2
        self._offset = QPointF(self.width() / 2 - cx * self._scale,
                               self.height() / 2 - cy * self._scale)
        self.update()

    # ------------------------------------------------------------------
    # 绘制
    # ------------------------------------------------------------------

    def paintEvent(self, e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), self._BG)
        if self._empty:
            p.setPen(self._EMPTY_COLOR)
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter,
                       "暂无记忆——先和 AI 对话产生记忆，再点「刷新」")
            return
        # 中心放射光晕（放射源视觉：柔光从中心向外扩散，配合放射布局）
        center = self._to_screen(QPointF(0.0, 0.0))
        radius = max(self.width(), self.height()) * 0.6
        grad = QRadialGradient(center, radius)
        grad.setColorAt(0.0, QColor(140, 100, 200, 50))
        grad.setColorAt(0.35, QColor(90, 140, 210, 18))
        grad.setColorAt(1.0, QColor(0, 0, 0, 0))
        p.fillRect(self.rect(), grad)
        # 用户连边：簇心 → 该 user 每个记忆（灰色实线）
        if self._user_centers:
            uc = QColor(self._EDGE_COLOR)
            uc.setAlpha(100)
            p.setPen(QPen(uc, 1.6))
            for user, center in self._user_centers.items():
                cc = self._to_screen(center)
                for i in self._user_groups.get(user, []):
                    p.drawLine(cc, self._to_screen(self._nodes[i]["pos"]))
        # 用户簇心：圆形节点（与记忆节点同款样式），暖青色 + 半径更大
        # 作区别；圆心显示用户名（太长截断）；悬停/选中光晕同记忆节点
        if self._user_centers:
            p.save()
            f = p.font()
            f.setBold(True)
            f.setPixelSize(13)
            p.setFont(f)
            fm = QFontMetricsF(f)
            for user, center in self._user_centers.items():
                cc = self._to_screen(center)
                ur = self._USER_NODE_R
                col = self._USER_COLOR
                if user == self._hover_user or user == self._selected_user:
                    halo = QColor(col)
                    halo.setAlpha(80)
                    p.setPen(Qt.PenStyle.NoPen)
                    p.setBrush(halo)
                    p.drawEllipse(cc, ur + 6, ur + 6)
                    p.setBrush(col)
                    p.setPen(QPen(QColor(255, 255, 255), 1.6))
                    p.drawEllipse(cc, ur, ur)
                else:
                    fill = QColor(col)
                    fill.setAlpha(70)
                    p.setBrush(fill)
                    p.setPen(QPen(col, 1.6))
                    p.drawEllipse(cc, ur, ur)
                # 用户名截断居中（self → AI 名字显示）
                label = _display_user(user)
                if fm.horizontalAdvance(label) > ur * 2 - 12:
                    while label and fm.horizontalAdvance(label + "…") > ur * 2 - 12:
                        label = label[:-1]
                    label += "…"
                p.setPen(QColor(255, 255, 255))
                p.drawText(QRectF(cc.x() - ur, cc.y() - ur, ur * 2, ur * 2),
                           Qt.AlignmentFlag.AlignCenter, label)
            p.restore()
        # 节点
        r = self._NODE_R
        for i, nd in enumerate(self._nodes):
            c = self._to_screen(nd["pos"])
            col = nd["color"]
            if i == self._hover or i == self._selected:
                # 悬停/选中：外圈光晕 + 实心亮色
                halo = QColor(col)
                halo.setAlpha(80)
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(halo)
                p.drawEllipse(c, r + 6, r + 6)
                p.setBrush(col)
                p.setPen(QPen(QColor(255, 255, 255), 1.6))
                p.drawEllipse(c, r, r)
                p.setPen(QPen(QColor(255, 255, 255), 1.0))
            else:
                fill = QColor(col)
                fill.setAlpha(60)
                p.setBrush(fill)
                p.setPen(QPen(col, 1.6))
                p.drawEllipse(c, r, r)
                p.setPen(QPen(QColor(70, 70, 70), 1.0))
            # 名称截断居中（优先内容摘要，哈希名仅作后备）
            fm = QFontMetricsF(p.font())
            label = nd.get("label") or nd["name"]
            if fm.horizontalAdvance(label) > r * 2 - 10:
                while label and fm.horizontalAdvance(label + "…") > r * 2 - 10:
                    label = label[:-1]
                label += "…"
            p.drawText(QRectF(c.x() - r, c.y() - r, r * 2, r * 2),
                       Qt.AlignmentFlag.AlignCenter, label)

    # ------------------------------------------------------------------
    # 交互（全部自绘处理，绝对可靠）
    # ------------------------------------------------------------------

    def mousePressEvent(self, e) -> None:
        if e.button() == Qt.MouseButton.MiddleButton:
            # 按住中键拖动 → 平移视图（不移动节点）
            self._panning = True
            self._pan_start = e.position()
            self._pan_offset0 = self._offset
            self.update()
        elif e.button() == Qt.MouseButton.LeftButton:
            idx = self._hit_node(e.position())
            if idx >= 0:
                self._selected = idx
                self._click_idx = idx
                self._drag_idx = idx
                self._drag_moved = False
                self._selected_user = None
            else:
                u = self._hit_user(e.position())
                if u is not None:
                    # 簇心（用户圆）：按下可整体拖动该 user 的放射星；
                    # 未拖动松开 → 单击查看该 user 的记忆汇总
                    self._selected_user = u
                    self._click_user = u
                    self._drag_user = u
                    self._drag_moved = False
                    self._drag_start_w = self._to_world(e.position())
                    self._drag_user_center0 = self._user_centers[u]
                    self._drag_user_nodes0 = {
                        i: self._nodes[i]["pos"]
                        for i in self._user_groups.get(u, [])}
                else:
                    self.blankClicked.emit()
            self.update()
        # 必须 accept：QWidget 默认 ignore 会触发 Qt 把鼠标事件传播给父级，
        # 父级非交互控件被窗口拖动过滤器命中 → 拖节点/点空白变成拖动整个窗口
        e.accept()

    def mouseMoveEvent(self, e) -> None:
        h = self._hit_node(e.position())
        hu = None if h >= 0 else self._hit_user(e.position())
        if h != self._hover or hu != self._hover_user:
            self._hover = h
            self._hover_user = hu
            self.setCursor(Qt.CursorShape.PointingHandCursor
                           if (h >= 0 or hu is not None)
                           else Qt.CursorShape.ArrowCursor)
            # 悬停节点 → tooltip（记忆全文）；悬停簇心 → tooltip（用户记忆数）
            if h >= 0:
                self.setToolTip(self._nodes[h]["tip"])
            elif hu is not None:
                n = len(self._user_groups.get(hu, []))
                self.setToolTip(_tip_html(
                    f"用户 {_display_user(hu)}",
                    f"{n} 条记忆（点击查看汇总）"))
            else:
                self.setToolTip("")
            self.update()
        if self._drag_idx >= 0:
            # 拖动节点（世界坐标跟随鼠标，连线随重绘更新）
            self._nodes[self._drag_idx]["pos"] = self._to_world(e.position())
            self._drag_moved = True
            self.update()
        elif self._drag_user is not None:
            # 拖动簇心：该 user 的放射星整体平移（簇心 + 所有记忆节点，
            # 相对位置不变，保持放射形状）
            delta = self._to_world(e.position()) - self._drag_start_w
            self._user_centers[self._drag_user] = self._drag_user_center0 + delta
            for i, p0 in self._drag_user_nodes0.items():
                self._nodes[i]["pos"] = p0 + delta
            self._drag_moved = True
            self.update()
        elif self._panning:
            d = e.position() - self._pan_start
            self._offset = QPointF(self._pan_offset0.x() + d.x(),
                                   self._pan_offset0.y() + d.y())
            self.update()
        e.accept()  # 同上：阻止忽略事件向父级传播触发窗口拖动

    def mouseReleaseEvent(self, e) -> None:
        if e.button() == Qt.MouseButton.LeftButton:
            if self._click_idx >= 0:
                # 单击（按下后未拖动）→ 通知控制中心弹窗看内容
                if not self._drag_moved:
                    self.nodeClicked.emit(self._nodes[self._click_idx])
                self._click_idx = -1
            self._drag_idx = -1
            if self._click_user is not None:
                # 簇心：未拖动松开 → 单击查看记忆汇总；已拖动 → 仅落位
                if not self._drag_moved:
                    self.userClicked.emit(self._click_user)
                self._click_user = None
            self._drag_user = None
        if e.button() == Qt.MouseButton.MiddleButton:
            self._panning = False
        e.accept()

    def mouseDoubleClickEvent(self, e) -> None:
        # 双击不再单独处理：单击已弹窗（避免与单击弹窗叠加重复弹出）
        e.accept()

    def wheelEvent(self, e) -> None:
        factor = 1.15 if e.angleDelta().y() > 0 else 1 / 1.15
        new_s = min(max(self._scale * factor, self._MIN_SCALE), self._MAX_SCALE)
        anchor = e.position()
        wa = self._to_world(anchor)
        self._scale = new_s
        # 保持鼠标下的世界点不动（围绕光标缩放）
        self._offset = QPointF(anchor.x() - wa.x() * new_s,
                               anchor.y() - wa.y() * new_s)
        self.update()
        e.accept()  # 滚轮同样需要 accept，避免传播到父级（父级滚动区/窗口）

    def resizeEvent(self, e) -> None:
        super().resizeEvent(e)
        # 首次显示（隐藏 tab 变大）或窗口缩放时重新适配
        if self._nodes and not self._panning and self._drag_idx < 0 \
                and self._drag_user is None:
            self._fit()
