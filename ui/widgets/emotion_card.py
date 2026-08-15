"""情绪绑定卡片：表情绑定区与动作绑定区各自独立成区域。"""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QLabel, QVBoxLayout

from ui.utils.constants import _DRAG_MIME_EXPR, _DRAG_MIME_MOTION
from ui.widgets.slot_label import _SlotLabel, _as_list


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

    def set_bound(self, value) -> None:
        """更新绑定显示：已绑定（单个或列表）显示内容，未绑定恢复虚线占位。"""
        items = _as_list(value)
        text = "、".join(items)
        if self._kind == "expr":
            self._slot.set_value(text, text if text else "拖拽表情到此绑定")
        else:
            self._slot.set_value(text, text if text else "拖拽动作到此绑定")
