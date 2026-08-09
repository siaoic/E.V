"""AI 桌宠控制中心（UI 由 Qt Designer 的 control_center.ui 定义）。

用法：python -m ui.control_center

界面（布局/样式）严格参考 live-2d(2)/test222.ui，定义在 control_center.ui，
可用 Qt Designer 直接打开编辑（保持 objectName 不变即可无缝加载）。

页面功能：
- 启动页：运行模式选择（vtuber / pet）→ 启动/停止主程序；实时日志；
  底部输入框可直接对话（等价于控制台 stdin）
- LLM 配置页 / 设置页：读写 .env
- 表情与动作页：紫色背景大区 + 6 个虚线情绪卡片（2 行 3 列）+
  4 列橙粉渐变表情按钮（点击预览 / 拖上去即固定绑定）
"""

import glob
import json
import os
import re
import sys
from typing import Dict, List

# 桌宠依赖（PySide6 等）装在项目内 vendor_pet/，避免污染系统环境。
# 打包后（sys.frozen）依赖已内嵌，无需也不能再用 __file__ 找 vendor_pet
#（__file__ 在打包后指向临时解压目录）。
if not getattr(sys, "frozen", False):
    _vendor = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "vendor_pet",
    )
    if os.path.isdir(_vendor) and _vendor not in sys.path:
        sys.path.insert(0, _vendor)

from PySide6.QtCore import (
    QEasingCurve, QEvent, QMimeData, QObject,
    QPoint, QProcess, QProcessEnvironment, QPropertyAnimation, Qt,
    Signal,
)
from PySide6.QtGui import (QBrush, QColor, QDrag, QIcon, QTextCursor)
from PySide6.QtUiTools import QUiLoader
from PySide6.QtWidgets import (
    QAbstractScrollArea, QApplication, QButtonGroup, QComboBox, QDialog,
    QFrame, QGraphicsDropShadowEffect, QGridLayout,
    QHBoxLayout, QHeaderView, QLabel, QMessageBox, QPlainTextEdit,
    QPushButton, QSizePolicy, QTableWidget, QTableWidgetItem, QVBoxLayout,
    QWidget,
)

from src.utils import config, console
from src.memory import memory
from ui.launcher import _update_env
from src.memory.memory_graph import MemoryGraphWidget, _display_user
from src.pet.emotion_actor import scan_model3
from src.pet.motion_files import (
    _MOTION_FILE_GROUP, _motion_base_name, _scan_motion_files)

# 情绪与动作页使用的 6 个基础情绪（固定写死，不再依赖 emotion_actor.EMOTIONS）
EMOTIONS: tuple = ("开心", "生气", "疑惑", "悲伤", "害怕", "厌恶")

UI_FILE = (
    os.path.join(sys._MEIPASS, "ui", "control_center.ui")
    if getattr(sys, "frozen", False)
    else os.path.join(os.path.dirname(os.path.abspath(__file__)), "control_center.ui")
)

# 窗口图标（任务栏）：exe 资源图标不会自动成为窗口图标，须显式 setWindowIcon。
# favicon.ico 通过 spec datas 打进 _MEIPASS/ui/，源码运行直接读 ui/ 下。
ICON_FILE = (
    os.path.join(sys._MEIPASS, "ui", "favicon.ico")
    if getattr(sys, "frozen", False)
    else os.path.join(os.path.dirname(os.path.abspath(__file__)), "favicon.ico")
)


def _save_ui_system_prompt(text: str) -> None:
    """保存控制中心 UI 人设到 ui/data/system_prompt.txt。

    SYSTEM_PROMPT_FILE 未配置时，src/utils/config.py 会自动读取该文件作为人设
    （路径与 config._UI_SYSTEM_PROMPT_FILE 保持一致，含 PyInstaller frozen 模式）。
    """
    path = config._UI_SYSTEM_PROMPT_FILE
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
    except OSError as e:
        console.error(f"保存 UI 人设失败：{e}")

# 主程序 stdout 的 ANSI 颜色/控制码（\x1b[90m、\x1b[?25h 等），写入日志控件前剥离
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def _env_defaults() -> Dict[str, str]:
    """直播弹幕字段的代码默认值（与 src/utils/config.py 保持一致）。

    保存配置时值等于默认 → 不写入 .env（.env 只保留自定义配置，避免冗余行）。
    """
    cfg = config.cfg
    return {
        "BILI_ENABLED": "true",
        "BILI_ROOM_ID": "0",
        "BILI_SESSDATA": "",
        "BILI_SERVER_PORT": "8766",
        "STT_BASE_URL": cfg.SILICONFLOW_BASE_URL or "https://api.siliconflow.cn/v1",
        "STT_MODEL": "FunAudioLLM/SenseVoiceSmall",
    }


def _remove_env_key(key: str) -> None:
    """从 .env 移除某 key 的配置行（值等于默认时清理冗余行，保留其它注释）。"""
    path = os.path.join(config.cfg.PROJECT_ROOT, ".env")
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.read().splitlines(keepends=True)
    except OSError:
        return
    kept = [line for line in lines if not line.strip().startswith(key + "=")]
    if len(kept) == len(lines):
        return
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.writelines(kept)
    except OSError:
        pass


def _update_env_skip_default(key: str, value: str, default: str) -> None:
    """写 .env 时跳过默认值：值为空或与代码默认相同 → 不写入（移除冗余行）。

    留空字段 = 使用代码回退默认（如 STT 回退共用 SiliconFlow），
    写空行既无效果又违背「.env 只保留自定义配置」。
    """
    v = (value or "").strip()
    if not v or v == (default or "").strip():
        _remove_env_key(key)
    else:
        _update_env(key, value)


def _find_project_root() -> str:
    """frozen 时定位项目根：从 exe 所在目录向上逐级找含 main.py 的目录。

    打包只包含 UI 启动器（无 VtuberMain.exe），主程序以源码方式运行，
    依赖项目根（.env / main.py / runtime venv）。exe 可放在项目根
    或任意子目录（如 dist/），找不到 main.py 返回空串。
    """
    cur = os.path.dirname(sys.executable)
    while True:
        if os.path.isfile(os.path.join(cur, "main.py")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            return ""
        cur = parent

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
    except (OSError, ValueError, TypeError):
        return None


# 全局 tooltip 样式：暖米白圆角卡片（对齐控制中心整体风格），作用于
# 全应用所有 QToolTip——记忆表格/图谱节点/工具表等悬浮提示统一美化。
_TOOLTIP_QSS = (
    "QToolTip {"
    " background-color: rgb(252, 250, 245);"
    " color: rgb(70, 65, 55);"
    " border: 1px solid rgba(160, 155, 145, 200);"
    " border-radius: 8px;"
    " padding: 8px 10px;"
    " font-size: 12px;"
    " font-family: \"微软雅黑\";"
    "}"
)


# ---------- 记忆详情弹窗（点击图谱节点 / 记忆条目） ----------

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
    _LAYER_COLORS = {
        "core": (140, 100, 200),        # 紫：核心身份
        "state": (70, 140, 210),        # 蓝：关系状态
        "preference": (230, 130, 60),   # 橙：长期偏好
        "archive": (90, 160, 100),      # 绿：历史摘要
    }
    _LAYER_NAMES = {
        "core": "核心身份", "state": "关系状态",
        "preference": "长期偏好", "archive": "历史摘要",
    }

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

        # 1. 标题行：记忆名 + 右上标签行
        title_row = QHBoxLayout()
        title_row.setSpacing(8)
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

    def _chips(self, node: dict, user: str, track: str) -> List:
        """构建标签 chips：层级（description 前缀）→ 归属（user）→ 类型（track）。"""
        chips = []
        # 层级标签（description 前缀 core/state/preference/archive）
        prefix = (node.get("description") or "").split("/")[0].strip().lower()
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


class ConfirmDialog(QDialog):
    """通用确认弹窗：与记忆详情弹窗同款暖色卡片风格。

    无边框圆角米白卡片 + 阴影 + 淡入上移动画；标题 / 正文 / 按钮文字
    可配，confirm 按钮支持危险色（红）或常规色。exec() 返回 Accepted /
    Rejected（确认/取消都有淡出动画，完成后才真正关闭）。

    用法：
        dlg = ConfirmDialog(self, "删除记忆", "确定删除？", confirm_text="删除")
        if dlg.exec() == ConfirmDialog.DialogCode.Accepted:
            ...
    """

    _FADE_MS = 240
    _FADE_OUT_MS = 160
    _RISE_PX = 14

    def __init__(self, parent, title: str, message: str,
                 confirm_text: str = "确定", cancel_text: str = "取消",
                 danger: bool = True) -> None:
        super().__init__(parent)
        self.setWindowFlags(
            Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setModal(True)
        self.resize(420, 220)
        self.setMinimumSize(380, 190)
        self._build_ui(title, message, confirm_text, cancel_text, danger)
        self._apply_style()

    def _build_ui(self, title: str, message: str,
                  confirm_text: str, cancel_text: str, danger: bool) -> None:
        card = QFrame(self)
        card.setObjectName("dialogCard")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(28, 28, 28, 28)
        outer.addWidget(card)

        lay = QVBoxLayout(card)
        lay.setContentsMargins(26, 24, 26, 20)
        lay.setSpacing(0)

        t = QLabel(title, card)
        t.setObjectName("titleLabel")
        t.setWordWrap(True)
        lay.addWidget(t)

        divider = QFrame(card)
        divider.setObjectName("divider")
        divider.setFixedHeight(1)
        lay.addSpacing(14)
        lay.addWidget(divider)

        msg = QLabel(message, card)
        msg.setObjectName("descLabel")
        msg.setWordWrap(True)
        msg.setAlignment(Qt.AlignmentFlag.AlignTop)
        lay.addSpacing(12)
        lay.addWidget(msg, 1)

        btns = QHBoxLayout()
        btns.setSpacing(8)
        btns.addStretch(1)
        cancel_btn = QPushButton(cancel_text, card)
        cancel_btn.setObjectName("closeBtn")
        cancel_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        cancel_btn.clicked.connect(self._on_cancel)
        btns.addWidget(cancel_btn)
        confirm_btn = QPushButton(confirm_text, card)
        confirm_btn.setObjectName("deleteBtn" if danger else "closeBtn")
        confirm_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        confirm_btn.setDefault(True)
        confirm_btn.setFocus()
        confirm_btn.clicked.connect(self._on_confirm)
        btns.addWidget(confirm_btn)
        lay.addSpacing(16)
        lay.addLayout(btns)

        shadow = QGraphicsDropShadowEffect(card)
        shadow.setBlurRadius(24)
        shadow.setOffset(0, 6)
        shadow.setColor(QColor(0, 0, 0, 60))
        card.setGraphicsEffect(shadow)

    def _apply_style(self) -> None:
        self.setStyleSheet(
            "QDialog { background: transparent; }"
            "#dialogCard { background-color: rgb(252, 250, 245);"
            " border-radius: 14px; }"
            "#titleLabel { color: rgb(40, 35, 25); font-size: 15px;"
            " font-weight: bold; font-family: \"微软雅黑\"; }"
            "#descLabel { color: rgb(70, 65, 55); font-size: 13px;"
            " font-family: \"微软雅黑\"; }"
            "#divider { background-color: rgba(140, 135, 125, 60); }"
            "#closeBtn { background-color: rgb(237, 232, 220);"
            " color: rgb(114, 95, 77); border: none; border-radius: 10px;"
            " padding: 6px 24px; font-size: 13px;"
            " font-family: \"微软雅黑\"; }"
            "#closeBtn:hover { background-color: rgb(228, 220, 203); }"
            "#closeBtn:pressed { background-color: rgb(218, 208, 190); }"
            "#deleteBtn { background-color: rgba(196, 86, 76, 30);"
            " color: rgb(176, 70, 62); border: none; border-radius: 10px;"
            " padding: 6px 24px; font-size: 13px;"
            " font-family: \"微软雅黑\"; }"
            "#deleteBtn:hover { background-color: rgba(196, 86, 76, 55); }"
            "#deleteBtn:pressed { background-color: rgba(196, 86, 76, 80); }"
        )

    def _on_confirm(self) -> None:
        self._fade_out_and_close(self.accept)

    def _on_cancel(self) -> None:
        self._fade_out_and_close(self.reject)

    def _fade_out_and_close(self, done) -> None:
        """淡出后调用 done（accept / reject），动画期间事件循环仍处理 exec。"""
        out = QPropertyAnimation(self, b"windowOpacity", self)
        out.setDuration(self._FADE_OUT_MS)
        out.setEasingCurve(QEasingCurve.Type.InCubic)
        out.setStartValue(1.0)
        out.setEndValue(0.0)
        out.finished.connect(done)
        out.start()

    def _play_show(self) -> None:
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

    def showEvent(self, e) -> None:
        super().showEvent(e)
        if not getattr(self, "_animated", False):
            self._animated = True
            self._play_show()


# ---------- 表情与动作页：拖拽绑定控件 ----------

# 拖拽数据类型：表情与动作分开，拖到情绪卡片时按类型绑到对应槽位
_DRAG_MIME_EXPR = "application/x-vtuber-expr"
_DRAG_MIME_MOTION = "application/x-vtuber-motion"

class _DragButton(QPushButton):
    """可拖拽按钮基类：点击试播 + 按住拖动发绑定拖拽。

    表情按钮（_DRAG_MIME_EXPR，橙粉渐变）与动作卡片（_DRAG_MIME_MOTION，
    蓝色渐变）共用拖拽交互，仅 mime/样式 objectName 不同；由上方 _SlotLabel
    按 mime 类型接收并绑到对应槽位。
    """

    def __init__(self, value: str, on_preview, mime: str, object_name: str,
                 parent=None):
        super().__init__(parent)
        self.value = value
        self._on_preview = on_preview
        self._mime = mime
        self._press_pos = None
        self._dragging = False
        self.setObjectName(object_name)
        self.setText(value)
        self.clicked.connect(lambda: self._on_preview(self.value))

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._press_pos = e.position().toPoint()
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if (self._press_pos is not None and not self._dragging
                and (e.position().toPoint() - self._press_pos).manhattanLength()
                >= QApplication.startDragDistance()):
            self._dragging = True
            self.setDown(False)          # 拖动不算点击，避免拖完触发试播
            self._begin_drag()
            self._dragging = False
            self._press_pos = None
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        self._press_pos = None
        super().mouseReleaseEvent(e)

    def _begin_drag(self) -> None:
        md = QMimeData()
        md.setData(self._mime, self.value.encode("utf-8"))
        drag = QDrag(self)
        drag.setMimeData(md)
        drag.setPixmap(self.grab())
        drag.setHotSpot(drag.pixmap().rect().center())
        drag.exec(Qt.DropAction.MoveAction)


class _SlotLabel(QLabel):
    """情绪卡片内的拖放槽位（表情/动作通用）：未绑定虚线占位，
    拖拽悬停高亮，放入即绑定。kind 决定样式（expr=紫 / motion=蓝）。"""

    def __init__(self, emotion: str, on_bind, mime: str, kind: str, parent=None):
        super().__init__(parent)
        self._emotion = emotion
        self._on_bind = on_bind
        self._mime = mime
        self._kind = kind
        self.setObjectName(f"slot_empty_{kind}")
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setAcceptDrops(True)
        self.setMinimumHeight(40)
        self.setWordWrap(True)

    def _set_drag_hover(self, on: bool) -> None:
        self.setProperty("drag", "true" if on else "false")
        self.style().unpolish(self)
        self.style().polish(self)

    def dragEnterEvent(self, e):
        if e.mimeData().hasFormat(self._mime):
            e.acceptProposedAction()
            self._set_drag_hover(True)
        else:
            e.ignore()

    def dragMoveEvent(self, e):
        if e.mimeData().hasFormat(self._mime):
            e.acceptProposedAction()
        else:
            e.ignore()

    def dragLeaveEvent(self, e):
        self._set_drag_hover(False)
        super().dragLeaveEvent(e)

    def dropEvent(self, e):
        self._set_drag_hover(False)
        if not e.mimeData().hasFormat(self._mime):
            return
        value = bytes(e.mimeData().data(self._mime)).decode("utf-8", "replace")
        self._on_bind(self._emotion, value)
        e.acceptProposedAction()

    def set_value(self, value: str, text: str) -> None:
        """更新绑定显示：已绑定显示内容，未绑定恢复虚线占位（objectName 切换样式）。"""
        self.setObjectName(f"slot_filled_{self._kind}" if value
                           else f"slot_empty_{self._kind}")
        self.setText(text)
        self.style().unpolish(self)
        self.style().polish(self)


class _EmotionCard(QFrame):
    """情绪绑定卡片：表情绑定区与动作绑定区各自独立成区域（2 行 3 列网格），
    不再一个卡片双槽混排。kind="expr" 只含表情槽（紫），kind="motion"
    只含动作槽（蓝），由 on_bind 回调绑到对应映射。"""

    _MIME = {"expr": _DRAG_MIME_EXPR, "motion": _DRAG_MIME_MOTION}

    def __init__(self, emotion: str, on_bind, kind: str, parent=None):
        super().__init__(parent)
        self.emotion = emotion
        self._kind = kind
        self.setObjectName("motion_zone_card" if kind == "motion" else "emotion_card")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(8)
        name_lb = QLabel(emotion)
        name_lb.setObjectName("emotion_name")
        name_lb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._slot = _SlotLabel(emotion, on_bind, self._MIME[kind], kind)
        lay.addWidget(name_lb)
        lay.addWidget(self._slot, 1)

    def set_bound(self, value: str) -> None:
        """更新绑定显示：已绑定显示内容，未绑定恢复虚线占位。"""
        if self._kind == "expr":
            self._slot.set_value(value, value if value else "拖拽表情到此绑定")
        else:
            self._slot.set_value(value, value if value else "拖拽动作到此绑定")


class _ComboWheelGuard(QObject):
    """QComboBox 滚轮守卫：鼠标悬停（未点击）时滚轮会误改选中值（Qt 默认行为）。

    拦截 Wheel 事件并转交给最近的滚动区（QScrollArea 的 viewport），
    这样悬停在下拉框上滚动时，只会滚动所在的列表/区域，不会误改值。
    """

    def eventFilter(self, obj, event):
        if event.type() != QEvent.Type.Wheel:
            return False
        # 转给父链上最近的滚动区滚动；combo 自身不再处理滚轮
        node = obj.parentWidget()
        while node is not None:
            if isinstance(node, QAbstractScrollArea):
                QApplication.sendEvent(node.viewport(), event)
                return True
            node = node.parentWidget()
        return True


class _WindowDragFilter(QObject):
    """无边框窗口拖动：qApp 级事件过滤器，按住窗口空白区域即可拖动。

    交互控件（按钮/输入框/下拉框/文本框/列表/表格/滚动条等）不触发拖动；
    点击空白（QWidget/QLabel 等非交互区域）时调用系统级 startSystemMove()，
    由操作系统接管拖动（Windows 下实时跟随、事件不丢失，比手动 move 可靠）。
    """

    # 交互控件类型：点击不触发拖动
    _INTERACTIVE = (
        "QAbstractButton", "QAbstractSpinBox", "QComboBox", "QLineEdit",
        "QAbstractSlider", "QAbstractScrollArea",
    )

    def __init__(self, window, app) -> None:
        super().__init__(app)
        self._window = window
        app.installEventFilter(self)

    def eventFilter(self, obj, event):
        t = event.type()
        if t != QEvent.Type.MouseButtonPress or event.button() != Qt.MouseButton.LeftButton:
            return False
        if not isinstance(obj, QWidget) or obj.window() is not self._window:
            return False
        # 交互控件（含其子类）不触发拖动
        if self._inside_interactive(obj):
            return False
        try:
            handle = self._window.windowHandle()
            if handle is not None:
                handle.startSystemMove()
                return True
        except Exception:
            pass
        return False

    def _inside_interactive(self, obj) -> bool:
        """obj 自身或其任一祖先是否为交互控件。

        注意：点击 QTextEdit/QListWidget/QScrollArea 等滚动区内部时，鼠标
        事件落在其 viewport（普通 QWidget）上，只检查 obj 本身会漏掉——
        必须沿父链向上检查（否则点日志想复制内容却会把窗口拖走）。
        """
        node = obj
        while node is not None:
            if isinstance(node, self._interactive_classes()):
                return True
            # 记忆图谱自绘处理节点拖拽/空白平移：图谱内的按下事件
            # 必须交给控件自己，不能触发窗口拖动（否则拖节点变成拖窗口）
            if isinstance(node, MemoryGraphWidget):
                return True
            node = node.parentWidget()
        return False

    @staticmethod
    def _interactive_classes() -> tuple:
        """懒加载交互控件类型（含 QAbstractScrollArea 的 viewport 命中）。"""
        cls = []
        for name in _WindowDragFilter._INTERACTIVE:
            c = getattr(__import__("PySide6.QtWidgets", fromlist=[name]), name, None)
            if c is not None:
                cls.append(c)
        return tuple(cls)


class _ConfirmDialog(QDialog):
    """确认关闭弹窗：样式与「记忆详情弹窗」（MemoryDetailDialog）完全一致。

    复用同一套视觉语言：米白圆角卡片 + 深棕标题 + 棕正文 + 分隔线 +
    底部按钮（取消=米灰 closeBtn / 确认关闭=红系 deleteBtn），
    阴影 blur 24 / (0, 6) / 黑色 24% 透明度。exec() 返回
    Accepted（确认）或 Rejected（取消）。
    """

    def __init__(self, parent=None, title: str = "确认关闭",
                 text: str = "确定要关闭控制中心吗？") -> None:
        super().__init__(parent)
        self.setWindowFlags(
            Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.resize(360, 196)
        self.setMinimumSize(320, 160)

        # 卡片容器：圆角米白背景 + 阴影（四周 margins 为阴影留呼吸空间）
        card = QFrame(self)
        card.setObjectName("dialogCard")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(28, 28, 28, 28)
        outer.addWidget(card)

        lay = QVBoxLayout(card)
        lay.setContentsMargins(24, 22, 24, 18)
        lay.setSpacing(0)

        # 1. 标题
        title_label = QLabel(title, card)
        title_label.setObjectName("titleLabel")
        title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(title_label)

        # 2. 正文（居中，可换行）
        lay.addSpacing(12)
        body = QLabel(text, card)
        body.setObjectName("descLabel")
        body.setAlignment(Qt.AlignmentFlag.AlignCenter)
        body.setWordWrap(True)
        lay.addWidget(body, 1)

        # 3. 分隔线
        lay.addSpacing(14)
        divider = QFrame(card)
        divider.setObjectName("divider")
        divider.setFixedHeight(1)
        lay.addWidget(divider)

        # 4. 底部按钮行：取消靠最左、确认关闭靠最右（两端对齐）
        bottom = QHBoxLayout()
        bottom.setSpacing(16)
        btn_cancel = QPushButton("取消", card)
        btn_cancel.setObjectName("closeBtn")
        btn_cancel.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_cancel.clicked.connect(self.reject)
        btn_ok = QPushButton("确认关闭", card)
        btn_ok.setObjectName("deleteBtn")
        btn_ok.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_ok.setDefault(True)
        btn_ok.clicked.connect(self.accept)
        bottom.addWidget(btn_cancel)
        bottom.addStretch(1)
        bottom.addWidget(btn_ok)
        lay.addSpacing(14)
        lay.addLayout(bottom)

        # 阴影（与记忆弹窗一致）
        shadow = QGraphicsDropShadowEffect(card)
        shadow.setBlurRadius(24)
        shadow.setOffset(0, 6)
        shadow.setColor(QColor(0, 0, 0, 60))
        card.setGraphicsEffect(shadow)

        # 样式逐值与 MemoryDetailDialog._apply_style 保持一致
        self.setStyleSheet(
            "QDialog { background: transparent; }"
            "#dialogCard { background-color: rgb(252, 250, 245);"
            " border-radius: 14px; }"
            "#titleLabel { color: rgb(40, 35, 25); font-size: 15px;"
            " font-weight: bold; font-family: \"微软雅黑\"; }"
            "#descLabel { color: rgb(114, 95, 77); font-size: 13px;"
            " font-family: \"微软雅黑\"; }"
            "#divider { background-color: rgba(140, 135, 125, 60); }"
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
        )


class _CloseConfirmFilter(QObject):
    """关闭确认：窗口被关闭（Alt+F4 / 任务栏关闭）时先弹确认框。

    本窗口是无边框（FramelessWindowHint），没有系统 X 按钮，但 Alt+F4 /
    任务栏右键关闭等仍会触发 QCloseEvent。确认后放行，取消则忽略事件。
    过滤器只装在窗口上（不装 qApp 级，避免记忆详情弹窗等子窗口关闭时误弹）。
    """

    def __init__(self, window) -> None:
        super().__init__(window)
        self._window = window

    def eventFilter(self, obj, event) -> bool:
        if obj is not self._window or event.type() != QEvent.Type.Close:
            return False
        dlg = _ConfirmDialog(self._window)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            event.ignore()  # 取消：吞掉关闭事件，窗口保持打开
            return True
        return False  # 确认：放行默认关闭流程


class ControlCenter:
    """组合 QUiLoader 加载的 control_center.ui；控件通过属性代理访问。"""

    def __init__(self) -> None:
        self.ui = QUiLoader().load(UI_FILE)
        self.cfg = config.cfg
        self.proc: "QProcess | None" = None
        # 日志框用等宽字体：━ 边框与空格等宽，console.header 的标题才能真正居中
        # （微软雅黑非等宽，━ 比空格宽，居中偏移）
        from PySide6.QtGui import QFont
        self.ui.log.setFont(QFont("Consolas", 9))
        # 全局 tooltip 样式（QToolTip 是独立顶层窗口，QSS 需挂在 QApplication 上）
        app = QApplication.instance()
        if app is not None:
            app.setStyleSheet(_TOOLTIP_QSS)
        # 无边框圆角窗口（test222 风格）：去掉系统直角边框 + 透明背景，
        # 圆角渐变背景与描边由 QSS 绘制，四角才会真正显示为圆角。
        self.ui.setWindowFlag(Qt.FramelessWindowHint, True)
        self.ui.setAttribute(Qt.WA_TranslucentBackground, True)
        # 按住窗口空白区域拖动（qApp 级过滤器：页面/侧边栏/面板空白均可）
        self._drag = _WindowDragFilter(self.ui, QApplication.instance())
        # 关闭确认弹窗（Alt+F4 / 任务栏关闭时先确认，防止误关）
        self.ui.installEventFilter(_CloseConfirmFilter(self.ui))
        self._hide_scrollbars()
        self._init_signals()
        self._init_state()

    def __getattr__(self, name):
        # UI 控件代理：self.rb_vts → self.ui.rb_vts
        return getattr(self.ui, name)

    def show(self) -> None:
        self._adapt_to_screen()
        self.ui.show()
        # 工具表格：初始化时窗口未显示、说明列仍按 Stretch 挤在 .ui 初始
        # 窄视口里，resizeRowsToContents 行高会虚高（把卡片撑出窗口底部）。
        # 显示后视口真实，延迟重算行高（幂等）。
        from PySide6.QtCore import QTimer
        QTimer.singleShot(0, self._resize_tool_rows)
        QTimer.singleShot(200, self._resize_tool_rows)

    def _resize_tool_rows(self) -> None:
        """工具表格可见时按真实列宽重算行高（见 show 的说明）。"""
        table = self.table_tool_status
        if table.isVisible() and table.rowCount():
            table.resizeRowsToContents()

    def _adapt_to_screen(self) -> None:
        """窗口初始尺寸自适应屏幕分辨率 + 屏幕居中。

        小屏不超出可用区域，大屏保持设计尺寸；随后把窗口移到主屏
        可用区域正中央（避开任务栏）。
        """
        scr = QApplication.primaryScreen().availableGeometry()
        if scr is None:
            return
        w = min(940, int(scr.width() * 0.92))
        h = min(680, int(scr.height() * 0.9))
        self.ui.resize(w, h)
        self.ui.move(scr.x() + (scr.width() - w) // 2,
                     scr.y() + (scr.height() - h) // 2)

    def _hide_scrollbars(self) -> None:
        """隐藏界面全部滚动条（滚动功能保留：滚轮/键盘仍可滚动）。

        遍历所有后代 QAbstractScrollArea（表格/滚动区/文本区，含
        QTableWidget、QScrollArea、QPlainTextEdit 等）设为
        ScrollBarAlwaysOff——视觉清爽，长内容靠滚轮/拖动滚动。
        弹窗正文（MemoryDetailDialog）的滚动条由自身 QSS 隐藏。
        """
        from PySide6.QtWidgets import QAbstractScrollArea
        for w in self.ui.findChildren(QAbstractScrollArea):
            try:
                w.setVerticalScrollBarPolicy(
                    Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
                w.setHorizontalScrollBarPolicy(
                    Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            except RuntimeError:
                pass  # 控件已被销毁（切换页面时 Qt 可能延迟清理）

    # ---------- 初始化 ----------

    def _init_signals(self) -> None:
        # 导航互斥组（QUiLoader 不支持 .ui 内 buttonGroup，改为代码建立）
        self.nav_group = QButtonGroup(self.ui)
        self.nav_group.setExclusive(True)
        for btn in (self.nav_launch, self.nav_llm, self.nav_memory,
                    self.nav_settings, self.nav_face, self.nav_tools,
                    self.nav_about):
            self.nav_group.addButton(btn)
        self.nav_launch.clicked.connect(lambda: self.stack.setCurrentIndex(0))
        self.nav_llm.clicked.connect(lambda: self.stack.setCurrentIndex(1))
        self.nav_memory.clicked.connect(lambda: self.stack.setCurrentIndex(2))
        self.nav_settings.clicked.connect(lambda: self.stack.setCurrentIndex(3))
        self.nav_face.clicked.connect(lambda: self.stack.setCurrentIndex(4))
        self.nav_tools.clicked.connect(lambda: self.stack.setCurrentIndex(5))
        self.nav_about.clicked.connect(lambda: self.stack.setCurrentIndex(6))
        # 启动/清空日志仅启动页显示（其他页面保留关闭按钮）
        self.stack.currentChanged.connect(self._on_page_changed)
        self.btn_clear_log.clicked.connect(lambda: self.log.clear())
        self.btn_close.clicked.connect(self.ui.close)
        self.radio_pet.toggled.connect(self._on_mode_changed)
        self.btn_toggle.clicked.connect(self._toggle)
        self.btn_send.clicked.connect(self._send_text)
        self.input_edit.returnPressed.connect(self._send_text)
        # 底部全长「更新配置」按钮（对齐 test222 saveConfigButton）：保存全部配置
        self.btn_save_config.clicked.connect(self._save_config)
        # 表情与动作页（拖拽绑定）：表情库启动时按当前桌宠模型自动构建，
        # 绑定结果随「更新配置」一并保存，无需单独按钮
        # 下拉框滚轮守卫：悬停时滚动只滚列表，不误改选中值
        self._combo_wheel_guard = _ComboWheelGuard(self.ui)
        for c in self.ui.findChildren(QComboBox):
            c.installEventFilter(self._combo_wheel_guard)
        # 启动页：模型下拉切换即生效（写 .env + 桌宠模式运行中热切换 + 刷新表情动作库）
        self.combo_models.currentIndexChanged.connect(self._on_model_selected)
        # 工具屋：刷新状态（先重读 .env，再重建表格）
        self.btn_refresh_tools.clicked.connect(self._refresh_tools)
        # 设置页：B站直播弹幕总开关 → 置灰/恢复下方字段
        self.cb_bili_enabled.toggled.connect(self._sync_bili_enabled_state)

    def _refresh_tools(self) -> None:
        """工具屋「刷新状态」：重读 .env / mcp_config.json 后重建表格。"""
        config.reload_tool_runtime()
        self._fill_tool_status()
        self._log("[控制中心] 工具状态已刷新\n")

    def _on_page_changed(self, idx: int) -> None:
        """启动/清空日志按钮只在启动页显示（其余页面保留关闭按钮）。"""
        launch = idx == 0
        self.btn_toggle.setVisible(launch)
        self.btn_clear_log.setVisible(launch)
        # 进入工具屋：按真实视口重算工具表格行高（首次显示时修正启动阶段
        # 的虚高，保证之后点「刷新」表格不会突然变化）
        if idx == 5:
            from PySide6.QtCore import QTimer
            QTimer.singleShot(0, self._resize_tool_rows)

    def _init_state(self) -> None:
        # 运行模式
        if self.cfg.RUN_MODE == "pet":
            self.radio_pet.setChecked(True)
        else:
            self.radio_vts.setChecked(True)
        # 桌宠模型列表
        self._scan_models()
        # LLM 配置页回填
        self.ed_key.setText(self.cfg.LLM_API_KEY or "")
        self.ed_url.setText(self.cfg.LLM_BASE_URL or "")
        self.ed_model.setText(self.cfg.LLM_MODEL or "")
        self.ed_prompt.setPlainText(self.cfg.SYSTEM_PROMPT or "")
        # Embedding 配置回填（记忆检索/情绪嵌入，本地 llama.cpp 或云端）
        self.ed_emb_url.setText(self.cfg.EMBEDDING_BASE_URL or "")
        self.ed_emb_model.setText(self.cfg.EMBEDDING_MODEL or "")
        self.ed_emb_key.setText(self.cfg.EMBEDDING_API_KEY or "")
        # 管家模型（ButlerAgent 记忆管家）回填
        self.ed_butler_url.setText(self.cfg.BUTLER_BASE_URL or "")
        self.ed_butler_model.setText(self.cfg.BUTLER_MODEL or "")
        self.ed_butler_key.setText(self.cfg.BUTLER_API_KEY or "")
        # 设置页回填
        self.cb_mcp.setChecked(bool(self.cfg.TOOLS_ENABLED))
        self.cb_proactive.setChecked(bool(self.cfg.PROACTIVE_ENABLED))
        self.cb_filter.setChecked(bool(self.cfg.PROFANITY_FILTER_ENABLED))
        self.cb_stt.setChecked(bool(self.cfg.STT_ENABLED))
        self.ed_tts_audio.setText(self.cfg.GPTSOVITS_REF_AUDIO or "")
        self.ed_tts_text.setText(self.cfg.GPTSOVITS_PROMPT_TEXT or "")
        self.ed_stt_key.setText(self.cfg.STT_API_KEY or "")
        self.ed_stt_url.setText(self.cfg.STT_BASE_URL or "")
        self.ed_stt_model.setText(self.cfg.STT_MODEL or "")
        self.cb_emotion_actor.setChecked(bool(self.cfg.EMOTION_ACTOR_ENABLED))
        # B站直播弹幕配置回填
        self.cb_bili_enabled.setChecked(bool(self.cfg.BILI_ENABLED))
        self.ed_bili_room.setText(
            str(self.cfg.BILI_ROOM_ID) if self.cfg.BILI_ROOM_ID else "")
        self.ed_bili_sessdata.setText(self.cfg.BILI_SESSDATA or "")
        self.ed_bili_port.setText(str(self.cfg.BILI_SERVER_PORT))
        self._sync_bili_enabled_state()
        # 表情与动作页
        self._init_face_page()
        self._apply_face_mode_state()
        # 工具屋（模型切换 / 扫描 / 备份还原）
        self._init_tools_page()
        # 记忆页（渲染 SQLite 中的记忆文件）
        self._init_memory_page()

    # ---------- 启动页逻辑 ----------

    def _list_models(self) -> List[str]:
        """扫描 live2d 文件夹下全部 .model3.json（相对项目根目录路径）。"""
        rels: List[str] = []
        base = os.path.join(self.cfg.PROJECT_ROOT, "live2d")
        if not os.path.isdir(base):
            return rels
        for p in sorted(glob.glob(os.path.join(base, "**", "*.model3.json"), recursive=True)):
            rel = os.path.relpath(p, self.cfg.PROJECT_ROOT).replace("\\", "/")
            if rel not in rels:
                rels.append(rel)
        return rels

    def _scan_models(self) -> None:
        # 只扫描 live2d 文件夹（用户指定的模型目录）。
        # blockSignals：初始化填充下拉会触发 currentIndexChanged，防误切模型
        rels = self._list_models()
        self.combo_models.blockSignals(True)
        try:
            self.combo_models.clear()
            if rels:
                self.combo_models.addItems(rels)
            current = self.cfg.PET_MODEL_PATH or ""
            if current and current not in rels:
                self.combo_models.addItem(current)
            idx = self.combo_models.findText(current) if current else 0
            self.combo_models.setCurrentIndex(max(0, idx))
        finally:
            self.combo_models.blockSignals(False)

    def _on_mode_changed(self, pet_selected: bool) -> None:
        self.combo_models.setEnabled(pet_selected)

    def _sync_bili_enabled_state(self) -> None:
        """B站直播弹幕总开关：关闭时禁用下方字段（置灰）。"""
        enabled = self.cb_bili_enabled.isChecked()
        for w in (self.ed_bili_room, self.ed_bili_sessdata, self.ed_bili_port):
            w.setEnabled(enabled)

    # ---------- 启动页：模型切换（选择即生效，含表情动作刷新） ----------

    def _on_model_selected(self, index: int) -> None:
        """启动页模型下拉切换：立即生效。

        写 .env + 更新内存配置；桌宠模式主程序运行中 → 向 stdin 发 !model 命令
        热切换（main.py 消费 → PetWidget.switch_model 重建）；未运行则下次启动生效。
        切换后刷新表情动作页（新模型的表情库/动作库）。
        """
        if index < 0:
            return
        path = self.combo_models.currentText().strip()
        if not path:
            return
        try:
            _update_env("PET_MODEL_PATH", path)
        except OSError as e:
            self._log(f"[控制中心] 写入 .env 失败：{e}\n")
            return
        self.cfg.PET_MODEL_PATH = path
        running = (self.proc is not None
                   and self.proc.state() == QProcess.ProcessState.Running)
        if running:
            # 运行中一律热推：桌宠模式真正换皮；vtuber 模式由主程序忽略
            self.proc.write(f"!model {path}\n".encode("utf-8"))
            self._log(f"[控制中心] 已发送模型热切换指令：{path}（立即生效）\n")
        else:
            self._log(f"[控制中心] 已保存模型 {path}（主程序未运行，下次启动生效）\n")
        # 刷新表情动作页（新模型的表情库/动作库）
        self._build_expr_library()
        self._build_action_library()

    # ---------- 工具屋（点击工具行启用/禁用，即存即生效） ----------

    def _init_tools_page(self) -> None:
        """初始化工具屋：5 列表格（操作/工具/类型/状态/说明），点击行即切换。"""
        table = self.table_tool_status
        table.setColumnCount(5)
        table.setHorizontalHeaderLabels(["操作", "工具", "类型", "状态", "说明"])
        table.verticalHeader().setVisible(False)
        table.setCornerButtonEnabled(False)  # 行头隐藏时不再画角块，避免盖住左上圆角边框
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        table.setAlternatingRowColors(True)
        table.setWordWrap(True)
        header = table.horizontalHeader()
        # 前四列固定宽度（Interactive）：列宽不随内容膨胀——resizeColumnsToContents
        # 按不换行文本测量，长工具名/状态会把列撑爆、总宽超出视口；而滚动条
        # 全局隐藏，右侧内容被裁切（「内框显示不完整」）。说明列 Stretch 吃满
        # 剩余空间，列宽总和恒等于视口。
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        table.setColumnWidth(0, 60)    # 操作
        table.setColumnWidth(1, 150)   # 工具
        table.setColumnWidth(2, 60)    # 类型
        table.setColumnWidth(3, 130)   # 状态
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)  # 说明
        self._tool_row_ids: List[str] = []
        self._tool_row_checkable: List[bool] = []
        self._tool_row_enabled: List[bool] = []
        table.cellClicked.connect(self._on_tool_cell_clicked)
        self._fill_tool_status()

    def _fill_tool_status(self) -> None:
        """重建工具状态表（含「操作」列），重新读取 .env / mcp_config.json。

        重建时保留滚动位置与选中行；行高只在表格可见时按真实列宽计算，
        不可见时延迟到进入工具屋页面再算——避免启动时（页面未显示）按
        窄视口算出行高虚高，之后点「刷新」表格突然变化。
        """
        table = self.table_tool_status
        vbar = table.verticalScrollBar()
        scroll_pos = vbar.value() if vbar is not None else 0
        cur_row = table.currentRow()
        rows = self._tool_rows()
        self._tool_row_ids = [r["id"] for r in rows]
        self._tool_row_checkable = [r["checkable"] for r in rows]
        self._tool_row_enabled = [r["enabled"] for r in rows]
        table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            # 列 0：操作（点击行切换启用/关闭；MCP 总开关关闭时服务器行不可切）
            act = QTableWidgetItem()
            act.setFlags(Qt.ItemIsEnabled)
            act.setTextAlignment(Qt.AlignCenter)
            if row["checkable"]:
                act.setText("启用" if row["enabled"] else "关闭")
                act.setForeground(QBrush(QColor(45, 130, 75)
                                         if row["enabled"]
                                         else QColor(150, 150, 150)))
            else:
                act.setText("—")
                act.setForeground(QBrush(QColor(200, 200, 200)))
            table.setItem(r, 0, act)
            # 列 1/2：名称、类型
            for c, text in enumerate((row["name"], row["kind"]), start=1):
                it = QTableWidgetItem(text)
                it.setFlags(Qt.ItemIsEnabled)
                table.setItem(r, c, it)
            # 列 3：状态（着色：已启用绿 / 未配置橙 / 已关闭灰）
            st = QTableWidgetItem(row["status"])
            st.setFlags(Qt.ItemIsEnabled)
            if row["status"].startswith("✅"):
                st.setForeground(QBrush(QColor(45, 130, 75)))
            elif row["status"].startswith("⚠️"):
                st.setForeground(QBrush(QColor(200, 120, 30)))
            else:
                st.setForeground(QBrush(QColor(130, 130, 130)))
            table.setItem(r, 3, st)
            # 列 4：说明
            ds = QTableWidgetItem(row["desc"])
            ds.setFlags(Qt.ItemIsEnabled)
            table.setItem(r, 4, ds)
        # 行高自适应内容（说明列换行显示完整，避免文字被裁切）；
        # 列宽已在 _init_tools_page 固定（说明列 Stretch），此处不再
        # resizeColumnsToContents——那会按不换行文本把列宽撑爆、总宽超视口
        if table.isVisible():
            table.resizeRowsToContents()
        else:
            # 页面未显示时按当前（窄）视口算出的行高会虚高，延迟到进入
            # 工具屋页面再按真实列宽重算（_resize_tool_rows 内部有可见性守卫）
            from PySide6.QtCore import QTimer
            QTimer.singleShot(0, self._resize_tool_rows)
        # 恢复滚动位置与选中行（刷新不跳顶、不丢选中，避免表格视觉跳动）
        if 0 <= cur_row < table.rowCount():
            table.setCurrentCell(cur_row, 0)
        if vbar is not None:
            vbar.setValue(scroll_pos)

    def _tool_rows(self) -> List[dict]:
        """收集工具行数据：(id, name, kind, enabled, checkable, status, desc)。

        id = 对应 .env 的 TOOL_*_ENABLED / MCP_ENABLED，或 "mcp_server:<json key>"
        （含 _disabled 后缀），点击切换时据此写回配置。
        """
        cfg = self.cfg
        rows: List[dict] = []

        def add(tid, name, kind, enabled, checkable, status, desc):
            rows.append({"id": tid, "name": name, "kind": kind,
                         "enabled": enabled, "checkable": checkable,
                         "status": status, "desc": desc})

        master_on = bool(cfg.TOOLS_ENABLED)  # 设置页「启动工具」总开关

        def add_local(tid, name, enabled, checkable, status, desc):
            """本地工具行：总开关（启动工具）关闭时强制显示已关、不可单独切换。"""
            if not master_on:
                enabled, checkable, status = False, False, "⭕ 总开关已关"
            add(tid, name, "本地", enabled, checkable, status, desc)

        # ---- 本地 Function Call 工具 ----
        if cfg.TOOL_WEB_SEARCH_ENABLED:
            if cfg.TAVILY_API_KEY:
                add_local("TOOL_WEB_SEARCH_ENABLED", "web_search", True, True,
                    "✅ 已启用", "联网搜索（Tavily）")
            else:
                add_local("TOOL_WEB_SEARCH_ENABLED", "web_search", True, True,
                    "⚠️ 未配置 key", "需在 .env 设置 TAVILY_API_KEY")
        else:
            add_local("TOOL_WEB_SEARCH_ENABLED", "web_search", False, True,
                "⭕ 已关闭", "联网搜索（Tavily）")
        if cfg.TOOL_GET_CURRENT_TIME_ENABLED:
            add_local("TOOL_GET_CURRENT_TIME_ENABLED", "get_current_time",
                True, True, "✅ 已启用", "获取当前时间（无外部依赖）")
        else:
            add_local("TOOL_GET_CURRENT_TIME_ENABLED", "get_current_time",
                False, True, "⭕ 已关闭", "获取当前时间（无外部依赖）")
        if cfg.TOOL_GET_WEATHER_ENABLED:
            if cfg.OPENWEATHERMAP_API_KEY:
                add_local("TOOL_GET_WEATHER_ENABLED", "get_weather", True, True,
                    "✅ 已启用", "查询天气（OpenWeatherMap）")
            else:
                add_local("TOOL_GET_WEATHER_ENABLED", "get_weather", True, True,
                    "⚠️ 未配置 key", "需在 .env 设置 OPENWEATHERMAP_API_KEY")
        else:
            add_local("TOOL_GET_WEATHER_ENABLED", "get_weather", False, True,
                "⭕ 已关闭", "查询天气（OpenWeatherMap）")
        skill_n = self._count_skills()
        if cfg.TOOL_LOAD_SKILL_ENABLED:
            if skill_n > 0:
                add_local("TOOL_LOAD_SKILL_ENABLED", "load_skill", True, True,
                    f"✅ 已启用（{skill_n} 个技能）", "按需加载技能 SKILL.md")
            else:
                add_local("TOOL_LOAD_SKILL_ENABLED", "load_skill", True, True,
                    "⚠️ 无技能", "SKILLS_DIR 目录下没有 SKILL.md")
        else:
            add_local("TOOL_LOAD_SKILL_ENABLED", "load_skill", False, True,
                "⭕ 已关闭", "按需加载技能 SKILL.md")

        # ---- MCP（外部工具服务器） ----
        # MCP 子开关（.env MCP_ENABLED）与工具总开关（设置页「启动工具」）
        # 都开启时服务器才可启用；总开关关闭时全部显示「总开关已关」。
        mcp_on = master_on and bool(cfg.MCP_ENABLED)
        for key in self._all_mcp_servers():
            display = key[:-len("_disabled")] if key.endswith("_disabled") else key
            enabled = not key.endswith("_disabled")
            if not master_on:
                add(f"mcp_server:{key}", f"MCP：{display}", "MCP", False, False,
                    "⭕ 总开关已关", "设置页「启动工具」已关闭，开启后可启用")
            elif not mcp_on:
                add(f"mcp_server:{key}", f"MCP：{display}", "MCP", False, False,
                    "⭕ MCP 未启用", "需在 .env 开启 MCP_ENABLED")
            elif enabled:
                add(f"mcp_server:{key}", f"MCP：{display}", "MCP", True, True,
                    "✅ 已配置", "mcp_config.json 外部工具服务器")
            else:
                add(f"mcp_server:{key}", f"MCP：{display}", "MCP", False, True,
                    "⭕ 已禁用", "mcp_config.json 外部工具服务器（点击恢复）")
        return rows

    def _on_tool_cell_clicked(self, row: int, col: int) -> None:
        """点击工具行任意单元格 → 切换启用/关闭（不可切换的行忽略）。"""
        if row < 0 or row >= len(self._tool_row_ids):
            return
        if not self._tool_row_checkable[row]:
            return
        self._apply_tool_toggle(row, not self._tool_row_enabled[row])

    def _apply_tool_toggle(self, row: int, checked: bool) -> None:
        """启用/关闭一个工具：写 .env（或 mcp_config.json），失败则回滚显示。"""
        tid = self._tool_row_ids[row]
        try:
            if tid.startswith("mcp_server:"):
                key = tid[len("mcp_server:"):]
                if not self._toggle_mcp_server(key, checked):
                    raise OSError(
                        "MCP 服务器重命名失败（同名冲突或配置文件不可写）")
                self._log(f"[控制中心] MCP 服务器已{'启用' if checked else '禁用'}："
                          f"{key[:-len('_disabled')] if key.endswith('_disabled') else key}\n")
            else:
                _update_env(tid, "true" if checked else "false")
                self._log(f"[控制中心] 工具开关已保存：{tid}="
                          f"{'true' if checked else 'false'}\n")
        except OSError as e:
            self._log(f"[控制中心] 写入配置失败：{e}\n")
            # 回滚显示（配置未写入，重建表格恢复原状）
            self._fill_tool_status()
            return
        # 本进程配置刷新（工具屋/设置页显示用），随后重建表格与状态列
        config.reload_tool_runtime()
        if getattr(self.ui, "cb_mcp", None) is not None:
            self.cb_mcp.setChecked(bool(self.cfg.TOOLS_ENABLED))
        self._fill_tool_status()
        self._notify_main_tools()

    def _toggle_mcp_server(self, key: str, enabled: bool) -> bool:
        """启/停一个 MCP 服务器：在 mcp_config.json 中加/去 _disabled 后缀。

        幂等：key 已是目标状态或已不存在（被改名）时直接返回 True。
        """
        path = self.cfg.MCP_CONFIG_PATH
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            return False
        if enabled:
            if not key.endswith("_disabled"):
                return True  # 已是启用状态
            if key not in data:
                return True  # 旧 key 已被改名/删除，视为已恢复
            target = key[:-len("_disabled")]
            if target in data:  # 原名已被占用，改名冲突
                return False
            data[target] = data.pop(key)
        else:
            if key.endswith("_disabled"):
                return True  # 已是禁用状态
            if key not in data:
                return True  # 旧 key 已被改名/删除，视为已禁用
            target = key + "_disabled"
            if target in data:  # 目标名已被占用，改名冲突
                return False
            data[target] = data.pop(key)
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except OSError:
            return False
        return True

    def _all_mcp_servers(self) -> List[str]:
        """读取 mcp_config.json 的全部服务器 key（含 _disabled 后缀的禁用项）。"""
        try:
            with open(self.cfg.MCP_CONFIG_PATH, "r", encoding="utf-8") as f:
                return sorted(json.load(f).keys())
        except (OSError, json.JSONDecodeError):
            return []

    def _notify_main_tools(self) -> None:
        """主程序运行中 → 发 !tools 命令热生效；未运行则下次启动生效。"""
        running = (self.proc is not None
                   and self.proc.state() == QProcess.ProcessState.Running)
        if running:
            self.proc.write(b"!tools\n")
            self._log("[控制中心] 已通知主程序热更新工具配置\n")
        else:
            self._log("[控制中心] 主程序未运行，工具配置将在下次启动生效\n")

    def _count_skills(self) -> int:
        """统计 SKILLS_DIR（逗号分隔多根）下的技能数（<技能名>/SKILL.md）。"""
        base = (self.cfg.SKILLS_DIR or "").strip()
        roots = [r.strip() for r in base.split(",") if r.strip()] if base else []
        total = 0
        for root in roots:
            d = root if os.path.isabs(root) else os.path.join(
                self.cfg.PROJECT_ROOT, root)
            if os.path.isdir(d):
                for entry in os.listdir(d):
                    if os.path.isfile(os.path.join(d, entry, "SKILL.md")):
                        total += 1
        return total

    def _toggle(self) -> None:
        """启动/停止切换：主程序未运行 → 启动；运行中 → 停止。"""
        if self.proc is not None and self.proc.state() != QProcess.NotRunning:
            self._stop()
        else:
            self._start()

    def _start(self) -> None:
        if self.proc is not None and self.proc.state() != QProcess.NotRunning:
            return
        mode = "pet" if self.radio_pet.isChecked() else "vtuber"
        # 打包只含 UI 启动器（无 VtuberMain.exe）：项目根（main.py 所在目录）
        # 可能 ≠ exe 目录，RUN_MODE / PET_MODEL_PATH 必须写进项目根 .env，
        # 主程序才能读到（frozen 时 config.PROJECT_ROOT 指向 exe 目录）。
        root = _find_project_root() if getattr(sys, "frozen", False) else ""
        if getattr(sys, "frozen", False) and not root:
            self._log("[控制中心] 未找到项目根（main.py），无法启动主程序。\n"
                      "请把 ControlCenter.exe 放在项目目录或其子目录（如 dist/）内。\n")
            return
        try:
            _update_env("RUN_MODE", mode, root=root)
            if mode == "pet":
                _update_env("PET_MODEL_PATH", self.combo_models.currentText().strip(),
                            root=root)
        except OSError as e:
            self._log(f"[控制中心] 写入 .env 失败：{e}\n")
            return

        env = QProcessEnvironment.systemEnvironment()
        env.insert("RUN_MODE", mode)
        if mode == "pet":
            # 关键：systemEnvironment() 会继承本进程旧环境（import config 时
            # load_dotenv 把当时的 PET_MODEL_PATH 写进了进程环境），子进程
            # load_dotenv 默认不覆盖已继承的环境变量 → 启动会加载旧模型。
            # 用最新值显式覆盖，保证「切模型 → 启动」立即生效，无需重启。
            env.insert("PET_MODEL_PATH", self.combo_models.currentText().strip())
        env.insert("PYTHONIOENCODING", "utf-8")
        env.insert("PYTHONUTF8", "1")
        env.insert("PYTHONUNBUFFERED", "1")  # 源码运行时逐行输出，日志实时滚动
        self.proc = QProcess()
        self.proc.setProcessEnvironment(env)
        self.proc.setWorkingDirectory(self.cfg.PROJECT_ROOT)
        self.proc.readyReadStandardOutput.connect(self._on_stdout)
        self.proc.readyReadStandardError.connect(self._on_stderr)
        self.proc.finished.connect(lambda *_: self._on_finished())
        # 启动失败（如 pythonw 缺失）必须在日志区可见，不能静默
        self.proc.errorOccurred.connect(
            lambda err: self._log(f"[控制中心] 启动主程序失败：{err}\n"))
        if getattr(sys, "frozen", False):
            # 打包环境只含 UI 启动器（不打包主程序）：用项目根 runtime venv
            # 的 pythonw 以源码方式启动 main.py。pythonw 是 windowed 子系统，
            # 不弹控制台黑框；QProcess 管道下 stdout/stderr/stdin 照常工作
            # （日志回显 + REPL 输入）。不用 python.exe——PySide6 的 QProcess
            # 没有 setCreateProcessArgumentsModifier，无法 CREATE_NO_WINDOW
            # 隐藏控制台窗口（该方法不存在，调用会抛 AttributeError）。
            py = os.path.join(root, "runtime", "Scripts", "pythonw.exe")
            if not os.path.isfile(py):
                self._log(f"[控制中心] 未找到 {py}，无法启动主程序。\n")
                return
            self.proc.setWorkingDirectory(root)
            self.proc.start(py, ["main.py"])
        else:
            self.proc.start(sys.executable, ["main.py"])

        self.btn_toggle.setText("停止")
        self._log(f"[控制中心] 已以「{mode}」模式启动\n")

    def _stop(self) -> None:
        """优雅停止：先发 /quit 让主程序走完归档/清理流程，超时未退出再强杀。

        直接 kill() 会跳过 main.py 的 finally——会话摘要/记忆蒸馏归档、TTS
        排空、MCP/STT 清理全部丢失（本轮对话记忆不落库）。先给 5 秒优雅窗口。
        """
        if self.proc is None or self.proc.state() == QProcess.NotRunning:
            return
        if getattr(self, "_stopping", False):
            return  # 已在停止流程中（防连点重复发 /quit、重复起定时器）
        self._stopping = True
        self._log("[控制中心] 正在优雅停止主程序…（等待记忆归档完成）\n")
        try:
            self.proc.write(b"/quit\n")
        except Exception:
            pass  # stdin 已关闭等极端情况：走下方超时强杀兜底
        from PySide6.QtCore import QTimer
        timer = QTimer(self.ui)
        timer.setSingleShot(True)
        timer.timeout.connect(self._force_kill)
        timer.start(5000)
        self._stop_kill_timer = timer

    def _force_kill(self) -> None:
        """优雅退出超时兜底：5 秒仍未退出则强制结束。"""
        if self.proc is not None and self.proc.state() != QProcess.NotRunning:
            self._log("[控制中心] 主程序未在 5 秒内退出，强制结束\n")
            try:
                self.proc.kill()
            except Exception:
                pass

    def _send_text(self) -> None:
        text = self.input_edit.text().strip()
        if not text:
            return
        if self.proc is not None and self.proc.state() == QProcess.ProcessState.Running:
            self.proc.write((text + "\n").encode("utf-8"))
            # 提示符「你 > 」由主程序打印并经 stdout 回显到日志，这里不再
            # 重复加前缀，否则同一行会显示「你 > 你 > how old are you」。
            self._log(f"{text}\n")
        else:
            self._log("[控制中心] 主程序未运行，无法发送\n")
        self.input_edit.clear()

    def _on_stdout(self) -> None:
        if self.proc is not None:
            self._log(bytes(self.proc.readAllStandardOutput()).decode("utf-8", "replace"))

    def _on_stderr(self) -> None:
        if self.proc is not None:
            self._log(bytes(self.proc.readAllStandardError()).decode("utf-8", "replace"))

    def _on_finished(self) -> None:
        self.btn_toggle.setText("启动")
        self._log("[控制中心] 主程序已退出\n")
        # 复位优雅停止状态并取消超时强杀定时器（进程已结束）
        self._stopping = False
        timer = getattr(self, "_stop_kill_timer", None)
        if timer is not None:
            timer.stop()
            self._stop_kill_timer = None

    def _log(self, text: str) -> None:
        # 主程序 stdout 的 console.* 输出带 ANSI 颜色码（\x1b[90m 等），
        # 日志控件不支持渲染，直接剥离避免显示 [0m[90m 等乱码
        text = _ANSI_RE.sub("", text)
        cur = self.log.textCursor()
        cur.movePosition(QTextCursor.End)
        cur.insertText(text)
        self.log.setTextCursor(cur)
        self.log.ensureCursorVisible()

    # ---------- 配置保存 ----------

    def _save_config(self) -> None:
        """底部「更新配置」：一次保存 LLM 配置 + 设置页全部字段到 .env，
        并把表情绑定映射一并写入 emotion_map.json。
        """

        def _bool(key: str, value: bool) -> None:
            _update_env(key, "true" if value else "false")

        # B站直播弹幕：房间号/端口必须是纯数字。非数字会被写进 .env，主程序
        # reload_config 里 int() 解析直接抛 ValueError（_dispatch 未捕获 → 崩溃
        # 整个主程序、连会话归档都丢）。在写入边界拦截并阻止保存。
        for key, v in (("BILI_ROOM_ID", self.ed_bili_room.text().strip()),
                       ("BILI_SERVER_PORT", self.ed_bili_port.text().strip())):
            if v and not v.isdigit():
                QMessageBox.warning(
                    self.ui, "配置错误", f"{key} 必须是纯数字，当前输入：{v}")
                return

        _saved = False   # .env 全部写盘成功后才发热更新命令（避免主程序读到旧值）
        _stt_changed = False   # 语音识别开关/Key 变化才发 !stt 热重启引擎
        _tools_changed = False   # 工具总开关变化才发 !tools 热启停/重启工具
        _emb_changed = False   # Embedding/管家模型变化：无法热更新，需重启主程序
        try:
            # LLM 配置
            _update_env("LLM_API_KEY", self.ed_key.text().strip())
            _update_env("LLM_BASE_URL", self.ed_url.text().strip())
            _update_env("LLM_MODEL", self.ed_model.text().strip())
            # Embedding 配置（记忆检索/情绪嵌入）
            _update_env("EMBEDDING_BASE_URL", self.ed_emb_url.text().strip())
            _update_env("EMBEDDING_MODEL", self.ed_emb_model.text().strip())
            _update_env("EMBEDDING_API_KEY", self.ed_emb_key.text().strip())
            # 管家模型（ButlerAgent 记忆管家）
            _update_env("BUTLER_BASE_URL", self.ed_butler_url.text().strip())
            _update_env("BUTLER_MODEL", self.ed_butler_model.text().strip())
            _update_env("BUTLER_API_KEY", self.ed_butler_key.text().strip())
            _emb_changed = (
                self.ed_emb_url.text().strip() != (self.cfg.EMBEDDING_BASE_URL or "")
                or self.ed_emb_model.text().strip() != (self.cfg.EMBEDDING_MODEL or "")
                or self.ed_emb_key.text().strip() != (self.cfg.EMBEDDING_API_KEY or "")
                or self.ed_butler_url.text().strip() != (self.cfg.BUTLER_BASE_URL or "")
                or self.ed_butler_model.text().strip() != (self.cfg.BUTLER_MODEL or "")
                or self.ed_butler_key.text().strip() != (self.cfg.BUTLER_API_KEY or ""))
            # 人设：SYSTEM_PROMPT_FILE 配置时人设来自 skill 文件夹（ed_prompt 只是预览）。
            # 未配置时，人设保存到 ui/data/system_prompt.txt（config 自动读取），
            # 不再写入 .env——多行未加引号会把 .env 撑成 5000+ 行无法解析
            # （python-dotenv 每次启动刷几百条 could not parse statement）。
            if not str(self.cfg.SYSTEM_PROMPT_FILE or "").strip():
                _save_ui_system_prompt(self.ed_prompt.toPlainText().strip())
            # 设置
            _bool("TOOLS_ENABLED", self.cb_mcp.isChecked())
            _bool("PROACTIVE_ENABLED", self.cb_proactive.isChecked())
            _bool("PROFANITY_FILTER_ENABLED", self.cb_filter.isChecked())
            _bool("STT_ENABLED", self.cb_stt.isChecked())
            # 语音识别相关变化（开关 / Key / URL / 模型）：变化才发 !stt 热重启引擎。
            # 空字段 = 回退代码默认（Key 空 = 回退共用 SiliconFlow；URL/模型空 =
            # 用代码默认），比较前先归一化为默认值——否则「留空恒不等于 cfg 里的
            # 默认字符串」会导致每次点保存都误判变化、反复热重启 STT 引擎。
            _defaults = _env_defaults()
            _stt_changed = (
                self.cb_stt.isChecked() != bool(self.cfg.STT_ENABLED)
                or (self.ed_stt_key.text().strip() or "")
                != (self.cfg.STT_API_KEY or "")
                or (self.ed_stt_url.text().strip() or _defaults["STT_BASE_URL"])
                != (self.cfg.STT_BASE_URL or "")
                or (self.ed_stt_model.text().strip() or _defaults["STT_MODEL"])
                != (self.cfg.STT_MODEL or ""))
            # Key 留空 = 回退共用 SiliconFlow：等于默认（空）时不写入 .env
            _update_env_skip_default(
                "STT_API_KEY", self.ed_stt_key.text().strip(), "")
            # 语音识别 URL / 模型：默认值不写入 .env（.env 只保留自定义配置）
            _update_env_skip_default(
                "STT_BASE_URL", self.ed_stt_url.text().strip(),
                _defaults["STT_BASE_URL"])
            _update_env_skip_default(
                "STT_MODEL", self.ed_stt_model.text().strip(),
                _defaults["STT_MODEL"])
            _bool("EMOTION_ACTOR_ENABLED", self.cb_emotion_actor.isChecked())
            _update_env("GPTSOVITS_REF_AUDIO", self.ed_tts_audio.text().strip())
            _update_env("GPTSOVITS_PROMPT_TEXT", self.ed_tts_text.text().strip())
            # B站直播弹幕：值等于代码默认 → 不写入 .env
            _update_env_skip_default(
                "BILI_ENABLED",
                "true" if self.cb_bili_enabled.isChecked() else "false",
                _defaults["BILI_ENABLED"])
            _update_env_skip_default(
                "BILI_ROOM_ID",
                self.ed_bili_room.text().strip() or "0",
                _defaults["BILI_ROOM_ID"])
            _update_env_skip_default(
                "BILI_SESSDATA", self.ed_bili_sessdata.text().strip(),
                _defaults["BILI_SESSDATA"])
            _update_env_skip_default(
                "BILI_SERVER_PORT",
                self.ed_bili_port.text().strip() or "8766",
                _defaults["BILI_SERVER_PORT"])
            # 工具总开关变化（!tools 热启停/重启 MCP 服务器并重新合并工具）
            _tools_changed = (
                self.cb_mcp.isChecked() != bool(self.cfg.TOOLS_ENABLED))
            _saved = True
        except OSError as e:
            console.error(f"保存失败：{e}")
        else:
            console.ok("配置已更新到 .env")
        # 默认待机动作 → .env（PET_IDLE_MOTION，随 !config 热更新立即生效）。
        # 空 = 自动（智能匹配待机）：等于默认值时不写入 .env（.env 只保留自定义配置）。
        # 情绪 → 表情/动作绑定已实时写入 _map_data（拖拽即更新），随映射一并保存。
        try:
            _update_env_skip_default(
                "PET_IDLE_MOTION",
                str(self.combo_idle_motion.currentData() or "").strip(), "")
        except OSError as e:
            console.error(f"保存默认待机动作失败：{e}")
        # 表情/动作绑定映射（与 .env 一起保存，点底部「更新配置」即可热生效）
        self._save_map()
        # —— 所有字段已写盘后才发热更新命令（先落盘后推送，避免主程序读到旧值） ——
        if not _saved:
            return
        # 刷新本进程 cfg：下次保存的「变化检测」（_stt_changed / _emb_changed /
        # _tools_changed）基于最新值，避免同一字段每次保存都被判为「已变化」
        # 而重复热更新（如 !stt 反复重启语音引擎）。
        config.reload_config()
        if self.proc is not None and self.proc.state() == QProcess.ProcessState.Running:
            # 统一热更新命令 !config：重读 .env 并热重建 LLM client / 主动对话 /
            # 内容过滤 / 记忆 / 桌宠窗口与待机 / 情绪映射，全部立即生效，无需重启。
            self.proc.write(b"!config\n")
            self._log("[控制中心] 配置已全部热更新（立即生效，无需重启）\n")
            # Embedding/管家模型：嵌入器与 ButlerAgent 仅在启动时构建，无法热更新
            if _emb_changed:
                self._log("[控制中心] Embedding/管家模型已保存，重启主程序后生效\n")
            # TTS 参考音频/文本（立即生效，无需重启）
            self.proc.write(
                f"!tts_audio {self.ed_tts_audio.text().strip()}\n".encode("utf-8"))
            self.proc.write(
                f"!tts_text {self.ed_tts_text.text().strip()}\n".encode("utf-8"))
            self._log("[控制中心] TTS 参考音频/文本已热更新（立即生效）\n")
            # 语音识别开关（!stt）：开关或 Key 变化时热启停/重启 STT 引擎
            if _stt_changed:
                self.proc.write(b"!stt\n")
                self._log("[控制中心] 语音识别配置已热更新（立即生效）\n")
            else:
                self._log("[控制中心] 语音识别配置未变化，跳过热更新\n")
            # 工具总开关（!tools）：变化时热启停/重启 MCP 服务器并重新合并工具，
            # 并同步刷新工具屋表格（本地工具行随总开关显示「总开关已关」）
            if _tools_changed:
                self.proc.write(b"!tools\n")
                self._log("[控制中心] 工具配置已热更新（立即生效）\n")
                config.reload_tool_runtime()
                self._fill_tool_status()
        # —— 保存确认反馈：按钮短暂变暗（不改变色/不弹 toast），随后还原 ——
        self._dim_save_btn()

    def _dim_save_btn(self) -> None:
        """保存成功：按钮文字变「已保存」+ 整体压暗（对比强烈，确认感同刷新按钮），
        1.2s 后还原。不用绿色。"""
        self.btn_save_config.setText("💾 已保存")
        self.btn_save_config.setStyleSheet(
            "QPushButton#btn_save_config{background:qlineargradient("
            "x1:0,y1:0,x2:1,y2:0,stop:0 rgba(115,110,100,255),"
            "stop:1 rgba(95,90,80,255));color:#ffffff;"
            "font-size:14px;padding:12px 20px;border-radius:10px;}")
        from PySide6.QtCore import QTimer
        QTimer.singleShot(1200, self._restore_save_btn)

    def _restore_save_btn(self) -> None:
        """还原「更新配置」按钮文字与样式（回到 .ui 全局 QSS）。"""
        self.btn_save_config.setText("💾 更新配置")
        self.btn_save_config.setStyleSheet("")

    # ---------- 表情与动作页 ----------

    def _init_face_page(self) -> None:
        """初始化表情动作页：整页滚动（scroll_face_page），两个独立绑定区域——
        表情绑定区（紫色大背景，6 个表情槽卡片 2 行 3 列）+ 动作绑定区
        （蓝色大背景，6 个动作槽卡片 2 行 3 列），各自配一键还原；
        下方橙粉表情按钮库 + 蓝色动作卡片库（有几个动作显示几个）。
        """
        self._emotions = EMOTIONS
        self._map_data = self._load_map_file()
        self._expr_cards: Dict[str, _EmotionCard] = {}
        self._motion_zone_cards: Dict[str, _EmotionCard] = {}
        self._expr_buttons: Dict[str, _DragButton] = {}
        self._motion_cards: Dict[str, _DragButton] = {}
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
        # 表情库按当前桌宠模型自动构建（模型在启动时已扫描，无需手动扫描）
        self._build_expr_library()
        # 动作库：默认待机动作下拉 + 动作卡片网格（有几个动作显示几个）
        self._build_action_library()
        # 隐藏滚动条（页面仍可滚轮滚动；滚动条遮挡内容且无样式）
        self.scroll_face_page.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll_face_page.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

    def _load_map_file(self) -> dict:
        path = self.cfg.EMOTION_MAP_FILE
        if not path or not os.path.isfile(path):
            return {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _build_expr_library(self) -> None:
        """按当前桌宠模型（PET_MODEL_PATH）自动构建表情库 + 回填已绑定表情。

        模型在启动时已扫描过，这里直接读 model3.json 即可，无需手动操作。
        """
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
        for emo, card in self._expr_cards.items():
            entry = self._map_data.get(emo) or {}
            expr = str(entry.get("expression") or "")
            card.set_bound(expr)
            # 已绑定表情固定到卡片：库中对应按钮隐藏（拖上去即固定）
            if expr and expr in self._expr_buttons:
                self._expr_buttons[expr].setVisible(False)
        self.label_map_status.setText(
            f"表情库已就绪：{len(info['expressions'])} 个表情")

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

    def _rebuild_button_library(self, layout, items: List[str],
                                preview_cb, mime: str, object_name: str,
                                prefix: str, empty_text: str,
                                with_tooltip: bool = False
                                ) -> Dict[str, _DragButton]:
        """重建可拖拽按钮库（表情/动作共用，动作区直接复用表情区逻辑）：
        清空容器 → 无内容显示空态提示 → 否则 4 列网格按钮（显示「前缀N」，
        悬停显示原名）；点击试播，按住拖动到上方对应槽位绑定。
        """
        self._clear_layout(layout)
        buttons: Dict[str, _DragButton] = {}
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
            btn = _DragButton(name, preview_cb, mime, object_name)
            btn.setText(f"{prefix}{i + 1}")   # 按钮统一显示「前缀N」
            if with_tooltip:
                btn.setToolTip(name)          # 悬停显示具体文件名/组名
            # 固定高度：网格会把行内多余空间全部分给按钮（垂直 policy 即使
            # 是 Fixed 也会被 QGridLayout 拉伸），只有一个按钮时会撑满整块
            # 显示区域；固定高度后按钮高度恒定、不随网格伸缩。
            btn.setFixedHeight(40)
            buttons[name] = btn
            grid.addWidget(btn, i // cols, i % cols)
        layout.addLayout(grid)
        return buttons

    def _rebuild_expr_library(self, expressions: List[str]) -> None:
        """重建下方表情库：4 列网格，按图编号（表情1/表情2…）；
        点击试播，按住拖动到上方表情槽绑定。
        """
        self._expr_buttons = self._rebuild_button_library(
            self.verticalLayout_expr_lib, expressions, self._preview_expr,
            _DRAG_MIME_EXPR, "expr_btn", "表情", "该模型没有可用的表情")

    # ---------- 动作绑定区域 ----------

    def _action_names(self) -> List[str]:
        """模型可用动作名列表：MotionFile 组文件（motion/ 子目录）优先，
        无自带动作文件时回退 model3.json 声明的 Motions 组（「组名 序号」）。"""
        rel = self.cfg.PET_MODEL_PATH or ""
        path = rel if os.path.isabs(rel) else os.path.join(
            self.cfg.PROJECT_ROOT, rel)
        names: List[str] = []
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
        # 默认待机动作下拉（回填已配置值）
        idle = self.combo_idle_motion
        idle.blockSignals(True)
        idle.clear()
        idle.addItem("自动（智能匹配待机）", "")
        for n in names:
            idle.addItem(n, n)
        cur = str(self.cfg.PET_IDLE_MOTION or "").strip()
        idx = idle.findData(cur)
        idle.setCurrentIndex(idx if idx >= 0 else 0)
        idle.blockSignals(False)
        # 动作卡片网格（复用表情区构建逻辑；悬停显示具体动作名）
        self._motion_cards = self._rebuild_button_library(
            self.verticalLayout_action_lib, names, self._preview_motion,
            _DRAG_MIME_MOTION, "motion_card", "动作", "该模型没有可用的动作",
            with_tooltip=True)
        if not names:
            self.label_map_status.setText(
                self.label_map_status.text() + "｜模型没有可绑定的动作")
            return
        # 动作绑定区回填已绑定动作
        for emo, card in self._motion_zone_cards.items():
            entry = self._map_data.get(emo) or {}
            motion = str(entry.get("motion") or "")
            card.set_bound(motion)
            # 已绑定动作固定到卡片：库中对应按钮隐藏（拖上去即固定）
            if motion and motion in self._motion_cards:
                self._motion_cards[motion].setVisible(False)

    def _entry(self, emotion: str) -> dict:
        """情绪映射条目（惰性创建）。"""
        return self._map_data.setdefault(emotion, {})

    def _on_bind_expr(self, emotion: str, expr: str) -> None:
        """拖拽绑定表情：按钮固定到情绪卡片（库中对应按钮隐藏），
        映射随「更新配置」一并保存。"""
        entry = self._entry(emotion)
        old = entry.get("expression")
        if expr:
            entry["expression"] = expr
        else:
            entry.pop("expression", None)
        # 固定：新绑定按钮从库中隐藏（拖上去即固定），旧绑定按钮恢复
        if old and old != expr and old in self._expr_buttons:
            self._expr_buttons[old].setVisible(True)
        if expr and expr in self._expr_buttons:
            self._expr_buttons[expr].setVisible(False)
        self._expr_cards[emotion].set_bound(expr)
        self.label_map_status.setText(
            f"已绑定：{emotion} → 表情 {expr}（点底部「更新配置」保存）")

    def _on_bind_motion(self, emotion: str, motion: str) -> None:
        """拖拽绑定动作：按钮固定到情绪卡片（库中对应按钮隐藏），
        映射随「更新配置」一并保存。"""
        entry = self._entry(emotion)
        old = entry.get("motion")
        if motion:
            entry["motion"] = motion
        else:
            entry.pop("motion", None)
        # 固定：新绑定按钮从库中隐藏（拖上去即固定），旧绑定按钮恢复
        if old and old != motion and old in self._motion_cards:
            self._motion_cards[old].setVisible(True)
        if motion and motion in self._motion_cards:
            self._motion_cards[motion].setVisible(False)
        self._motion_zone_cards[emotion].set_bound(motion)
        self.label_map_status.setText(
            f"已绑定：{emotion} → 动作 {motion}（点底部「更新配置」保存）")

    def _reset_expr(self) -> None:
        """一键还原：清空所有情绪的表情绑定（库中按钮全部恢复显示）。"""
        for emo in self._emotions:
            entry = self._map_data.get(emo)
            if entry:
                old = entry.pop("expression", None)
                if old and old in self._expr_buttons:
                    self._expr_buttons[old].setVisible(True)
            self._expr_cards[emo].set_bound("")
        self.label_map_status.setText("已还原全部表情绑定（点底部「更新配置」保存）")

    def _reset_action(self) -> None:
        """一键还原：清空所有情绪的动作绑定 + 默认待机动作恢复「自动」，
        库中按钮全部恢复显示。"""
        for emo in self._emotions:
            entry = self._map_data.get(emo)
            if entry:
                old = entry.pop("motion", None)
                if old and old in self._motion_cards:
                    self._motion_cards[old].setVisible(True)
            self._motion_zone_cards[emo].set_bound("")
        idx = self.combo_idle_motion.findData("")
        self.combo_idle_motion.setCurrentIndex(max(0, idx))
        self.label_map_status.setText("已还原全部动作绑定（点底部「更新配置」保存）")

    def _save_map(self) -> None:
        """把内存映射写入 data/emotion_map.json（由「更新配置」触发）。"""
        out = {emo: entry for emo, entry in self._map_data.items()
               if emo in self._emotions and entry}
        path = self.cfg.EMOTION_MAP_FILE
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(out, f, ensure_ascii=False, indent=2)
        except OSError as e:
            console.error(f"保存映射失败：{e}")
            return
        self._map_data = out
        console.ok(f"情绪映射已保存到 {os.path.relpath(path, self.cfg.PROJECT_ROOT)}"
                   "（点底部「更新配置」即可热生效）")

    def _preview_expr(self, name: str) -> None:
        """点击表情库按钮：试播该表情（向运行中的桌宠主程序发 /expr）。"""
        self._send_face_command(f"/expr {name}")

    def _preview_motion(self, name: str) -> None:
        """点击动作卡片：试播该动作（向运行中的桌宠主程序发 /motion）。"""
        self._send_face_command(f"/motion {name}")

    def _send_face_command(self, cmd: str) -> None:
        """试播：向运行中的主程序（桌宠模式）发送表情/动作命令。"""
        if self.proc is None or self.proc.state() != QProcess.ProcessState.Running:
            self._log("[控制中心] 主程序未运行（需以桌宠模式启动），无法试播\n")
            return
        self.proc.write((cmd + "\n").encode("utf-8"))
        # 同上：提示符由主程序打印回显，这里只记录命令本身，避免重复「你 > 」
        self._log(f"{cmd}\n")

    # ---------- 记忆页 ----------

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
            "点击节点查看记忆，点击用户名圆查看该用户记忆汇总，"
            "拖动记忆圆点调整位置，滚轮缩放，中键拖动平移")

    def _on_user_clicked(self, user: str) -> None:
        """图谱簇心（用户名圆）单击：弹窗汇总该 user 的所有记忆。"""
        mems = [f for f in self._mem_files
                if (f.get("user") or "chao") == user]
        body = "\n".join(
            f"· {str(m.get('content') or '').strip()}" for m in mems)
        if not body:
            body = "（该用户暂无记忆）"
        latest = max((m.get("updated_at") or m.get("created_at") or ""
                      for m in mems), default="")
        self.label_memory_status.setText(
            f"用户 {_display_user(user)}：{len(mems)} 条记忆")
        self._show_memory_detail({
            "name": _display_user(user),
            "description": f"用户 {_display_user(user)} 的记忆汇总",
            "content": body,
            "user": user,
            "track": "memory",
            "updated_at": latest,
        })

    def _on_graph_node_clicked(self, node: dict) -> None:
        """图谱节点单击：状态栏显示摘要 + 弹出该记忆详情弹窗。

        弹窗加防抖（同一节点 0.6s 内不重复弹），避免双击被当作
        两次单击而连续弹出两次。
        """
        import time
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

    def _apply_face_mode_state(self) -> None:
        """表情/动作控制仅对桌宠模式生效：非桌宠时禁用操作控件，但页面保持可滚动浏览。

        注意：scroll_face_page 本身不能 setEnabled(False)——禁用 QScrollArea 会
        连带禁用滚动条与滚轮滚动（页面无法浏览）。非桌宠只禁操作型控件。
        """
        pet = self.cfg.RUN_MODE == "pet"
        for w in (self.card_emotion_zone, self.card_expr_library,
                  self.card_action_bind, self.btn_reset_expr,
                  self.btn_reset_action):
            w.setEnabled(pet)
        if not pet:
            self.label_face_hint.setText(
                "表情/动作控制仅在桌宠模式（RUN_MODE=pet）下生效。\n"
                "请到启动页选择「桌面宠物」模式并重新启动后，本页功能可用。")
            self.label_face_hint.setVisible(True)
        else:
            # 桌宠模式下不显示说明文字（拖拽绑定/即时保存为直觉操作）
            self.label_face_hint.setVisible(False)


def main() -> None:
    # 任务栏图标（PyInstaller -w 打包的关键修复）：Windows 7+ 按进程的
    # AppUserModelID 分组显示任务栏图标。若未显式设置，-w 打包后的 exe 在
    # 任务栏可能显示为默认空白图标（即使 --icon 和 setWindowIcon 都做了）。
    # 必须在 QApplication 创建【之前】调用；ID 取本项目唯一值即可。
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "E.V.ControlCenter")
        except Exception:
            pass  # 设置失败不致命，仅任务栏可能回退默认图标
    app = QApplication(sys.argv)
    if os.path.exists(ICON_FILE):
        app.setWindowIcon(QIcon(ICON_FILE))
    win = ControlCenter()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
