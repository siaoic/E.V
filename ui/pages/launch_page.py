"""启动页（mixin）：模式选择 / 桌宠模型扫描切换 / 表情动作页联动。"""

from PySide6.QtCore import QProcess

from ui.utils import env_helpers
from ui.utils.path_helpers import _list_models


class LaunchPage:
    """启动页逻辑：运行模式切换、模型下拉选择即生效。"""

    def _scan_models(self) -> None:
        # 只扫描 live2d 文件夹（用户指定的模型目录）。
        # blockSignals：初始化填充下拉会触发 currentIndexChanged，防误切模型
        rels = _list_models(self.cfg.PROJECT_ROOT)
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
        """切换运行模式：桌宠模型下拉随模式启用；表情与动作页重建当前模式
        绑定库（桌宠读 model3.json，vtuber 读运行时 VTS 扫描缓存）。"""
        self.combo_models.setEnabled(pet_selected)
        self._update_face_lib_timer()
        if not getattr(self, "_expr_cards", None):
            return  # 表情页尚未初始化（_init_state 阶段），由 _init_face_page 统一构建
        # 映射文件按模式分离（emotion_map.json / emotion_map_vts.json），切换时重载
        self._map_data = self._load_map_file()
        self._build_expr_library()
        self._build_action_library()

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
            env_helpers._update_env("PET_MODEL_PATH", path)
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
