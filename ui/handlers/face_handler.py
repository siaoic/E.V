"""表情/动作绑定逻辑（mixin）：情绪映射文件的读写与绑定操作。"""

import json
import os

from PySide6.QtCore import QEvent
from PySide6.QtWidgets import QApplication

from src.utils import console
from ui.widgets.slot_label import _as_list


class FaceHandler:
    """表情/动作绑定映射逻辑：读映射文件、拖拽绑定/解除、一键还原、保存。"""

    def _load_map_file(self) -> dict:
        path = self._map_path()
        if not path or not os.path.isfile(path):
            return {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _current_mode(self) -> str:
        """当前运行模式（以启动页单选为准，切换无需重启）：pet / vtuber。"""
        return "pet" if self.radio_pet.isChecked() else "vtuber"

    def _map_path(self) -> str:
        """当前模式的情绪映射文件：桌宠 emotion_map.json，vtuber
        emotion_map_vts.json（与运行时 cfg.EMOTION_MAP_FILE 同一规则；
        .env 显式配置 EMOTION_MAP_FILE 时以其为准）。"""
        env_path = os.getenv("EMOTION_MAP_FILE") or ""
        if env_path:
            return env_path
        name = ("emotion_map_vts.json" if self._current_mode() == "vtuber"
                else "emotion_map.json")
        return os.path.join(self.cfg.PROJECT_ROOT, "data", name)

    def _entry(self, emotion: str) -> dict:
        """情绪映射条目（惰性创建）。"""
        return self._map_data.setdefault(emotion, {})

    def _refresh_bind_buttons(self) -> None:
        """按当前映射整体刷新绑定状态：更新全部情绪卡片的绑定显示，
        并重算表情/动作库按钮可见性（已绑定任意情绪的按钮隐藏）。

        只在拖拽结束（drag.exec 返回）后调用：Qt 规范禁止在拖拽进行中
        修改源 widget 可见性，否则布局刷新被吞、按钮「又显示出来」。
        """
        expr_binds = {emo: _as_list((self._map_data.get(emo) or {}).get("expression"))
                      for emo in self._emotions}
        motion_binds = {emo: _as_list((self._map_data.get(emo) or {}).get("motion"))
                        for emo in self._emotions}
        for emo, card in getattr(self, "_expr_cards", {}).items():
            card.set_bound(expr_binds[emo])
        for name, btn in getattr(self, "_expr_buttons", {}).items():
            btn.setVisible(not any(name in items for items in expr_binds.values()))
        for emo, card in getattr(self, "_motion_zone_cards", {}).items():
            card.set_bound(motion_binds[emo])
        for name, btn in getattr(self, "_motion_cards", {}).items():
            btn.setVisible(not any(name in items for items in motion_binds.values()))

    def _on_bind_expr(self, emotion: str, expr: str) -> None:
        """拖拽绑定表情：同一情绪可绑定多个表情（再拖同名项 = 解除），
        映射随「更新配置」一并保存；按钮可见性由拖拽结束统一刷新。"""
        entry = self._entry(emotion)
        items = _as_list(entry.get("expression"))
        if expr:
            if expr in items:
                items.remove(expr)  # 再拖一次 = 解除绑定
            else:
                items.append(expr)  # 追加绑定
        if items:
            entry["expression"] = items
        else:
            entry.pop("expression", None)
        status = "、".join(items) if items else "（无）"
        self.label_map_status.setText(
            f"已绑定：{emotion} → 表情 {status}（点底部「更新配置」保存）")

    def _on_bind_motion(self, emotion: str, motion: str) -> None:
        """拖拽绑定动作：同一情绪可绑定多个动作（再拖同名项 = 解除），
        映射随「更新配置」一并保存。"""
        entry = self._entry(emotion)
        items = _as_list(entry.get("motion"))
        if motion:
            if motion in items:
                items.remove(motion)  # 再拖一次 = 解除绑定
            else:
                items.append(motion)  # 追加绑定
        if items:
            entry["motion"] = items
        else:
            entry.pop("motion", None)
        # 固定：所有已绑定按钮从库中隐藏，其余恢复显示
        for name, btn in self._motion_cards.items():
            btn.setVisible(name not in items)
        self._motion_zone_cards[emotion].set_bound(items)
        status = "、".join(items) if items else "（无）"
        self.label_map_status.setText(
            f"已绑定：{emotion} → 动作 {status}（点底部「更新配置」保存）")

    @staticmethod
    def _clear_btn_visual(btn) -> None:
        """清除按钮悬停/按下视觉残留：隐藏后恢复显示的按钮可能残留
        WA_UnderMouse（拖拽/隐藏期间收不到 Leave 事件），导致 QSS :hover
        样式残留、按钮看起来「变暗」。发 Leave 事件强制清除并刷新样式。"""
        btn.setDown(False)
        QApplication.sendEvent(btn, QEvent(QEvent.Type.Leave))
        btn.style().unpolish(btn)
        btn.style().polish(btn)

    def _reset_expr(self) -> None:
        """一键还原：清空所有情绪的表情绑定（库中按钮全部恢复显示）。"""
        for emo in self._emotions:
            entry = self._map_data.get(emo)
            if entry:
                entry.pop("expression", None)
            self._expr_cards[emo].set_bound([])
        for btn in self._expr_buttons.values():
            btn.setVisible(True)
            self._clear_btn_visual(btn)
        self.label_map_status.setText("已还原全部表情绑定（点底部「更新配置」保存）")

    def _reset_action(self) -> None:
        """一键还原：清空所有情绪的动作绑定 + 默认待机动作恢复「自动」，
        库中按钮全部恢复显示。"""
        for emo in self._emotions:
            entry = self._map_data.get(emo)
            if entry:
                entry.pop("motion", None)
            self._motion_zone_cards[emo].set_bound([])
        for btn in self._motion_cards.values():
            btn.setVisible(True)
            self._clear_btn_visual(btn)
        idx = self.combo_idle_motion.findData("")
        self.combo_idle_motion.setCurrentIndex(max(0, idx))
        self.label_map_status.setText("已还原全部动作绑定（点底部「更新配置」保存）")

    def _save_map(self) -> None:
        """把内存映射写入当前模式的映射文件（由「更新配置」触发）。"""
        out = {emo: entry for emo, entry in self._map_data.items()
               if emo in self._emotions and entry}
        path = self._map_path()
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
