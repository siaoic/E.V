"""桌宠辅助模块：模块级辅助函数 + BubbleSub 字幕桥接类。

BubbleSub 类（独立顶层类）与字幕字体/动作文件扫描等纯函数放这里，
避免 core.py 被非 PetWidget 核心逻辑挤占行数。
这些函数/类从原 src/pet/widget.py 逐字搬来，逻辑零改动。
"""

from __future__ import annotations

import glob
import json
import os
from typing import List, Optional, Tuple

from PySide6.QtGui import QFont, QFontDatabase

# 自定义字幕字体文件（相对项目根目录 assets/ 下；打包后与 exe 同目录的 assets/ 亦可）
_SUBTITLE_FONT_FILE = os.path.join("assets", "ArtierEN-2.ttf")

# 模型目录自带的动作文件（model3.json 未声明的也收录）：
# 扫描 <模型目录>/motion 与 /motions 子目录下的 *.motion3.json，
# 运行时 LoadExtraMotion 注册为 MotionFile 组——「有什么用什么」。
_MOTION_FILE_GROUP = "MotionFile"
_MOTION_DIRS = ("motion", "motions")


def _load_subtitle_font() -> QFont:
    """加载自定义字幕字体（ArtierEN-2.ttf），失败回退微软雅黑。"""
    from ev.utils import config as _config

    candidates = [
        os.path.join(_config.cfg.PROJECT_ROOT, _SUBTITLE_FONT_FILE),
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "ArtierEN-2.ttf"),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "ArtierEN-2.ttf"),
    ]
    for p in candidates:
        if not os.path.isfile(p):
            continue
        fid = QFontDatabase.addApplicationFont(p)
        families = QFontDatabase.applicationFontFamilies(fid)
        if families:
            return QFont(families[0], 13)
        break
    return QFont("Microsoft YaHei", 13)


def _scan_motion_files(model3_path: str) -> List[str]:
    """模型目录 motion/motions 子目录下的 .motion3.json 文件（排序，绝对路径）。"""
    base = os.path.dirname(os.path.abspath(model3_path))
    files = []
    for d in _MOTION_DIRS:
        files.extend(glob.glob(os.path.join(base, d, "*.motion3.json")))
    return sorted(set(files))


def _motion_base_name(path: str) -> str:
    """动作文件显示/匹配名：去掉 .motion3.json / .motion3 / .json 后缀。

    控制中心「动作绑定区域」与桌宠播放端共用此命名（文件名去扩展名），
    避免 splitext 只去掉最后一层 .json 留下「Hiyori_m01.motion3」的脏名。
    """
    base = os.path.basename(path)
    for suf in (".motion3.json", ".motion3", ".json"):
        if base.lower().endswith(suf):
            return base[: -len(suf)]
    return base


def _declared_idle_file(_model_path_fn, cfg_project_root: str) -> str:
    """model3.json 声明的 Idle 组第一个动作文件绝对路径；无则空串。

    从原 PetWidget._declared_idle_file 提取为纯函数：
    - _model_path_fn: 零参数 callable，返回 model3.json 绝对路径。
    - cfg_project_root: 仅用于取目录，实际路径由 _model_path_fn 返回。
    """
    try:
        mpath = _model_path_fn()
        with open(mpath, "r", encoding="utf-8") as f:
            data = json.load(f)
        entries = data.get("FileReferences", {}).get("Motions", {}).get("Idle") or []
        if entries and entries[0].get("File"):
            return os.path.normpath(os.path.join(
                os.path.dirname(os.path.abspath(mpath)),
                str(entries[0]["File"]),
            ))
    except Exception:
        pass
    return ""


class BubbleSub:
    """把 PetWidget 的字幕接进 stream 的 sub 接口（push text/clear）。

    用法：`sub = BubbleSub(pet_widget)` 传给 main/stream，
    stream.py / proactive.py 无需感知渲染目标。
    """

    def __init__(self, widget) -> None:
        self.widget = widget

    def push(self, kind: str, text: str = "", speed_ms: int = 0) -> None:
        try:
            if kind == "text":
                self.widget.show_text(str(text), speed_ms)
            elif kind == "clear":
                self.widget.clear_text()
            # "user"（用户发言）在桌宠模式不显示气泡
        except Exception:
            pass

    def stop(self) -> None:
        """对齐 SubtitleServer 的生命周期接口（main.py finally 统一调用）。"""
        try:
            self.widget.clear_text()
        except Exception:
            pass
