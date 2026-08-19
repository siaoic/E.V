"""表情与动作页（mixin）：绑定区构建 / 表情动作库重建 / 试播。"""

import json
import os

from PySide6.QtCore import QProcess, Qt
from PySide6.QtWidgets import QGridLayout, QLabel

from src.pet.emotion_actor import scan_model3
from src.pet.motion_files import (
    _MOTION_FILE_GROUP, _motion_base_name, _scan_motion_files)
from ui.utils.constants import EMOTIONS, _DRAG_MIME_EXPR, _DRAG_MIME_MOTION
from ui.widgets.drag_button import _DragButton
from ui.widgets.emotion_card import _EmotionCard
from ui.widgets.slot_label import _as_list


class FacePage:
    """表情与动作页逻辑：6 情绪绑定卡片 + 表情/动作库 + 试播/绑定。"""

    # 表情与动作页在 stack 中的索引（导航映射见 control_center._init_signals）
    _FACE_PAGE_INDEX = 2

    def _init_face_page(self) -> None:
        """初始化表情动作页：整页滚动（scroll_face_page），两个独立绑定区域——
        表情绑定区（紫色大背景，6 个表情槽卡片 2 行 3 列）+ 动作绑定区
        （蓝色大背景，6 个动作槽卡片 2 行 3 列），各自配一键还原；
        下方橙粉表情按钮库 + 蓝色动作卡片库（有几个动作显示几个）。
        """
        self._emotions = EMOTIONS
        self._map_data = self._load_map_file()
        self._expr_cards: dict = {}
        self._motion_zone_cards: dict = {}
        self._expr_buttons: dict = {}
        self._motion_cards: dict = {}
        # 表情绑定区网格（2 行 3 列，只含表情槽）
        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(10)
        self.verticalLayout_emotion_zone.addLayout(grid)
        for i, emo in enumerate(EMOTIONS):
            card = _EmotionCard(emo, self._on_bind_expr, "expr")
            self._expr_cards[emo] = card
            grid.addWidget(card, i // 3, i % 3)
        # 动作绑定区网格（2 行 3 列，只含动作槽）
        grid_motion = QGridLayout()
        grid_motion.setHorizontalSpacing(10)
        grid_motion.setVerticalSpacing(10)
        self.verticalLayout_motion_zone.addLayout(grid_motion)
        for i, emo in enumerate(EMOTIONS):
            card = _EmotionCard(emo, self._on_bind_motion, "motion")
            self._motion_zone_cards[emo] = card
            grid_motion.addWidget(card, i // 3, i % 3)
        # 一键还原：表情区 / 动作区各自批量清空绑定
        self.btn_reset_expr.clicked.connect(self._reset_expr)
        self.btn_reset_action.clicked.connect(self._reset_action)
        # 表情库按当前模型自动构建（桌宠读 model3.json，vtuber 读运行时扫描缓存）
        self._build_expr_library()
        # 动作库：默认待机动作下拉 + 动作卡片网格（有几个动作显示几个）
        self._build_action_library()
        # 隐藏滚动条（页面仍可滚轮滚动；滚动条遮挡内容且无样式）
        self.scroll_face_page.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll_face_page.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

    def _vts_face_lib(self):
        """vtuber 模式表情/动作库：读运行时（VtsEmotionActor.scan）写入的
        扫描缓存 data/vts_face_lib.json。绑定名与运行时播放完全一致
        （VTS 可直接播放的表情 / 动画热键）；文件缺失或未扫描返回空。"""
        path = os.path.join(self.cfg.DATA_ROOT, "vts_face_lib.json")
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not data.get("model_name"):
                return [], [], ""
            return (data.get("expressions") or [],
                    data.get("motions") or [],
                    str(data.get("model_name", "")))
        except (OSError, ValueError):
            return [], [], ""

    def _update_face_lib_timer(self) -> None:
        """按当前模式启停表情库轮询（仅 vtuber 需要读运行时扫描缓存）。"""
        if not getattr(self, "_face_lib_timer", None):
            return  # 定时器在 __init__ 尾部创建（_init_state 阶段会先触发模式切换）
        if self._current_mode() == "vtuber":
            if not self._face_lib_timer.isActive():
                self._face_lib_timer.start()
        else:
            self._face_lib_timer.stop()

    def _refresh_face_lib(self) -> None:
        """重新读取当前模型的表达/动作库并重建（vtuber 读运行时扫描缓存）。"""
        if self._current_mode() != "vtuber":
            return
        self._build_expr_library()
        self._build_action_library()

    def _poll_face_lib(self) -> None:
        """vtuber 模式轮询绑定库缓存：运行时扫描/模型切换后自动重建绑定库。
        仅当表情页正在浏览且缓存内容（mtime+size）变化时重建，避免无谓刷新；
        不在表情页时的更新由进入页面时的 _on_page_changed 兜底。"""
        path = os.path.join(self.cfg.DATA_ROOT, "vts_face_lib.json")
        try:
            st = os.stat(path)
            key = (st.st_mtime_ns, st.st_size)
        except OSError:
            key = None
        if key == getattr(self, "_face_lib_key", None):
            return
        self._face_lib_key = key
        if self.stack.currentIndex() == self._FACE_PAGE_INDEX:
            self._refresh_face_lib()

    def _build_expr_library(self) -> None:
        """按当前模型自动构建表情库 + 回填已绑定表情。
        桌宠读 PET_MODEL_PATH 的 model3.json；vtuber 读运行时 VTS 扫描缓存。
        """
        if self._current_mode() == "vtuber":
            exprs, _motions, model_name = self._vts_face_lib()
            if not model_name:
                self.label_map_status.setText(
                    "VTS 表情库未就绪：请先启动主程序连接 VTubeStudio"
                    "（自动扫描后本页自动刷新）")
            else:
                self.label_map_status.setText(
                    f"表情库已就绪（{model_name}）：{len(exprs)} 个表情")
            self._rebuild_expr_library(exprs)
        else:
            rel = self.cfg.PET_MODEL_PATH or ""
            if not rel:
                self.label_map_status.setText("未配置桌宠模型（PET_MODEL_PATH）")
                self._rebuild_expr_library([])
                return
            path = rel if os.path.isabs(rel) else os.path.join(
                self.cfg.PROJECT_ROOT, rel)
            if not os.path.isfile(path):
                self.label_map_status.setText("模型文件不存在")
                self._rebuild_expr_library([])
                return
            info = scan_model3(path)
            self._rebuild_expr_library(info["expressions"])
            self.label_map_status.setText(
                f"表情库已就绪：{len(info['expressions'])} 个表情")
        # 回填已绑定表情（两模式共用）
        all_binds = {emo: _as_list((self._map_data.get(emo) or {}).get("expression"))
                     for emo in self._emotions}
        for emo, card in self._expr_cards.items():
            card.set_bound(all_binds[emo])
        # 已绑定表情固定到卡片：库中按钮只在「未绑定到任何情绪」时显示
        # （注意不能用逐情绪覆盖 setVisible，后一个会把前一个已隐藏的又显示出来）
        for name, btn in self._expr_buttons.items():
            btn.setVisible(not any(name in items for items in all_binds.values()))

    def _clear_layout(self, layout) -> None:
        """清空布局（含嵌套子布局）：widget 与子 layout 一并移除销毁。

        表情库/动作库按钮网格都是「addLayout(grid)」挂进容器，仅 takeAt
        widget 会漏掉子布局及其按钮，导致重建时旧按钮残留错位。
        """
        while (item := layout.takeAt(0)) is not None:
            w = item.widget()
            if w is not None:
                w.deleteLater()
                continue
            sub = item.layout()
            if sub is not None:
                self._clear_layout(sub)

    def _rebuild_button_library(self, layout, items: list,
                                preview_cb, mime: str, object_name: str,
                                empty_text: str) -> dict:
        """重建可拖拽按钮库（表情/动作共用，动作区直接复用表情区逻辑）：
        清空容器 → 无内容显示空态提示 → 否则 4 列网格按钮（直接显示真实名称）；
        点击试播，按住拖动到上方对应槽位绑定。
        """
        self._clear_layout(layout)
        buttons: dict = {}
        if not items:
            empty = QLabel(empty_text)
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(empty)
            return buttons
        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(10)
        cols = 4
        # 4 列均分宽度（stretch=1）：按钮宽度恒等于「区域宽/4」，
        # 即使某行只有 1 个按钮（如只有 1 个动作）也只占 1/4，不撑满整行。
        for c in range(cols):
            grid.setColumnStretch(c, 1)
        for i, name in enumerate(items):
            btn = _DragButton(name, preview_cb, mime, object_name,
                              on_drag_finished=self._refresh_bind_buttons)
            btn.setText(name)                  # 直接显示真实名称（表情/动作名）
            # 固定高度：网格会把行内多余空间全部分给按钮（垂直 policy 即使
            # 是 Fixed 也会被 QGridLayout 拉伸），只有一个按钮时会撑满整块
            # 显示区域；固定高度后按钮高度恒定、不随网格伸缩。
            btn.setFixedHeight(40)
            buttons[name] = btn
            grid.addWidget(btn, i // cols, i % cols)
        layout.addLayout(grid)
        return buttons

    def _rebuild_expr_library(self, expressions: list) -> None:
        """重建下方表情库：4 列网格，按钮直接显示表情名；
        点击试播，按住拖动到上方表情槽绑定。
        """
        self._expr_buttons = self._rebuild_button_library(
            self.verticalLayout_expr_lib, expressions, self._preview_expr,
            _DRAG_MIME_EXPR, "expr_btn", "该模型没有可用的表情")

    # ---------- 动作绑定区域 ----------

    def _action_names(self) -> list:
        """模型可用动作名列表。桌宠：MotionFile 组文件（motion/ 子目录）优先，
        无自带动作文件时回退 model3.json 声明的 Motions 组（「组名 序号」）；
        vtuber：VTS 动画热键名（读运行时扫描缓存）。"""
        if self._current_mode() == "vtuber":
            return list(self._vts_face_lib()[1])
        rel = self.cfg.PET_MODEL_PATH or ""
        path = rel if os.path.isabs(rel) else os.path.join(
            self.cfg.PROJECT_ROOT, rel)
        names: list = []
        if not os.path.isfile(path):
            return names
        names = [_motion_base_name(f) for f in _scan_motion_files(path)]
        if names:
            return names
        info = scan_model3(path)
        for group, count in info["motions"].items():
            if group == _MOTION_FILE_GROUP:
                continue
            names.extend(f"{group} {i}" for i in range(count))
        return names

    def _build_action_library(self) -> None:
        """构建动作库：默认待机动作下拉 + 动作卡片网格（有几个动作显示几个）。

        动作卡片样式与情绪区/表情库一致（蓝色系）：点击试播 /motion，
        按住拖动到上方情绪卡片的「动作槽」绑定（mime 与表情区分开）。
        按钮网格直接复用表情区的 _rebuild_button_library。
        """
        names = self._action_names()
        # 默认待机动作下拉（回填已配置值；默认待机为桌宠特性 PET_IDLE_MOTION，
        # vtuber 模式的待机由运行时 MOTION_PATH 控制，这里只保留「自动」）
        idle = self.combo_idle_motion
        idle.blockSignals(True)
        idle.clear()
        idle.addItem("自动（智能匹配待机）", "")
        if self._current_mode() != "vtuber":
            for n in names:
                idle.addItem(n, n)
        cur = str(self.cfg.PET_IDLE_MOTION or "").strip()
        idx = idle.findData(cur)
        idle.setCurrentIndex(idx if idx >= 0 else 0)
        idle.blockSignals(False)
        # 动作卡片网格（复用表情区构建逻辑；直接显示动作名）
        self._motion_cards = self._rebuild_button_library(
            self.verticalLayout_action_lib, names, self._preview_motion,
            _DRAG_MIME_MOTION, "motion_card", "该模型没有可用的动作")
        if not names:
            self.label_map_status.setText(
                self.label_map_status.text() + "｜模型没有可绑定的动作")
            return
        # 动作绑定区回填已绑定动作（与表情区同一修复：统一收集后整体设置
        # 按钮可见性，避免逐情绪覆盖把已隐藏按钮又显示出来）
        all_binds = {emo: _as_list((self._map_data.get(emo) or {}).get("motion"))
                     for emo in self._emotions}
        for emo, card in self._motion_zone_cards.items():
            card.set_bound(all_binds[emo])
        for name, btn in self._motion_cards.items():
            btn.setVisible(not any(name in items for items in all_binds.values()))

    def _preview_expr(self, name: str) -> None:
        """点击表情库按钮：试播该表情（向运行中的主程序发 /expr）。"""
        self._send_face_command(f"/expr {name}")

    def _preview_motion(self, name: str) -> None:
        """点击动作卡片：试播该动作（向运行中的主程序发 /motion）。"""
        self._send_face_command(f"/motion {name}")

    def _send_face_command(self, cmd: str) -> None:
        """试播：向运行中的主程序（桌宠/VTS 模式）发送表情/动作命令。"""
        if self.proc is None or self.proc.state() != QProcess.ProcessState.Running:
            self._log("[控制中心] 主程序未运行，无法试播\n")
            return
        self.proc.write((cmd + "\n").encode("utf-8"))
        # 同上：提示符由主程序打印回显，这里只记录命令本身，避免重复「你 > 」
        self._log(f"{cmd}\n")

    def _apply_face_mode_state(self) -> None:
        """表情/动作控制桌宠与 vtuber 模式均可用：页面控件全部启用。

        注意：scroll_face_page 本身不能 setEnabled(False)——禁用 QScrollArea 会
        连带禁用滚动条与滚轮滚动（页面无法浏览）。绑定库未就绪时由
        _build_expr_library 在状态栏给出提示。
        """
        for w in (self.card_emotion_zone, self.card_expr_library,
                  self.card_action_bind, self.btn_reset_expr,
                  self.btn_reset_action):
            w.setEnabled(True)
        # 两模式均不显示说明文字（拖拽绑定/即时保存为直觉操作）
        self.label_face_hint.setVisible(False)
