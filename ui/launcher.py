"""启动器：PySide6 图形界面，选择运行模式并启动主程序。

用法：python -m ui.launcher
- 选择 VTubeStudio 虚拟主播（vtuber）或桌面宠物（pet）
- 桌宠模式可修改模型路径
- 点击「启动」：把选择写入 .env 后以子进程运行 main.py（新开控制台窗口）
"""

import os
import subprocess
import sys

# 桌宠依赖（PySide6 等）装在项目内 vendor_pet/，避免污染系统环境
_vendor = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "vendor_pet",
)
if os.path.isdir(_vendor) and _vendor not in sys.path:
    sys.path.insert(0, _vendor)

from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from ev.utils import config
from ui.utils.env_helpers import _update_env


class Launcher(QWidget):
    """启动器主窗口。"""

    def __init__(self) -> None:
        super().__init__()
        self.cfg = config.cfg
        self.setWindowTitle("AI 虚拟主播启动器")
        self.setMinimumWidth(440)

        layout = QVBoxLayout(self)

        title = QLabel("AI 虚拟主播启动器")
        title.setStyleSheet("font-size: 20px; font-weight: bold;")
        layout.addWidget(title)

        # 运行模式
        mode_label = QLabel("运行模式：")
        layout.addWidget(mode_label)
        self.rb_vts = QRadioButton("VTubeStudio 虚拟主播（vtuber）")
        self.rb_pet = QRadioButton("桌面宠物（pet）")
        self.rb_vts.setChecked(self.cfg.RUN_MODE != "pet")
        self.rb_pet.setChecked(self.cfg.RUN_MODE == "pet")
        self.rb_pet.toggled.connect(self._on_mode_changed)
        layout.addWidget(self.rb_vts)
        layout.addWidget(self.rb_pet)

        # 桌宠模型路径（仅桌宠模式可编辑）
        form = QFormLayout()
        self.model_edit = QLineEdit(self.cfg.PET_MODEL_PATH)
        btn_browse = QPushButton("浏览…")
        btn_browse.clicked.connect(self._browse_model)
        row = QHBoxLayout()
        row.addWidget(self.model_edit)
        row.addWidget(btn_browse)
        form.addRow("桌宠模型：", row)
        layout.addLayout(form)

        self.btn_start = QPushButton("启动")
        self.btn_start.setMinimumHeight(36)
        self.btn_start.clicked.connect(self._start)
        layout.addWidget(self.btn_start)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        self._on_mode_changed(self.rb_pet.isChecked())

    # ---------- 交互 ----------

    def _on_mode_changed(self, pet_selected: bool) -> None:
        self.model_edit.setEnabled(pet_selected)

    def _browse_model(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "选择 Live2D 模型", self.cfg.PROJECT_ROOT, "Live2D 模型 (*.model3.json)")
        if path:
            self.model_edit.setText(path)

    def _start(self) -> None:
        mode = "pet" if self.rb_pet.isChecked() else "vtuber"
        try:
            _update_env("RUN_MODE", mode)
            if mode == "pet":
                _update_env("PET_MODEL_PATH", self.model_edit.text().strip())
        except OSError as e:
            self.status.setText(f"写入 .env 失败：{e}")
            return

        # 子进程继承环境变量（RUN_MODE 显式传入，与 .env 保持一致）
        env = dict(os.environ)
        env["RUN_MODE"] = mode
        cmd = [sys.executable, os.path.join(self.cfg.PROJECT_ROOT, "main.py")]
        try:
            creationflags = subprocess.CREATE_NEW_CONSOLE if os.name == "nt" else 0
            subprocess.Popen(
                cmd, cwd=self.cfg.PROJECT_ROOT, env=env, creationflags=creationflags)
        except OSError as e:
            self.status.setText(f"启动失败：{e}")
            return
        self.status.setText(
            f"已以「{mode}」模式启动，主程序在独立控制台窗口运行。\n"
            "（.env 已更新，之后直接运行 python main.py 也使用该模式）")
        self.status.setStyleSheet("color: #2e7d32;")


def main() -> None:
    app = QApplication(sys.argv)
    # 窗口图标：frozen 时从 _MEIPASS/ui/ 读（spec datas 打入），否则项目 ui/
    base = getattr(sys, "_MEIPASS", os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))
    icon = os.path.join(base, "ui", "favicon.ico")
    if os.path.exists(icon):
        app.setWindowIcon(QIcon(icon))
    win = Launcher()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
