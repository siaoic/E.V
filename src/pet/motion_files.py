"""桌宠动作文件纯文件助手（模块顶部只 stdlib，控制中心可安全导入）。

- `_MOTION_FILE_GROUP`：模型自带动作文件（motion/motions/animations 子目录）
  在渲染层归入的「动作组」名（由 widget 用 LoadExtraMotion 动态加载）。
- `_motion_base_name(path)`：去掉动作文件后缀得到可读名。
- `_scan_motion_files(model3_path)`：扫模型目录下所有 .motion3.json。
- `_read_motion_duration(path)`：读 motion3.json 的 Meta.Duration（秒）。
"""

import json
import os
from typing import List, Optional

# 模型自带动作文件（motion/animations 子目录）归入的动作组名
_MOTION_FILE_GROUP = "MotionFile"

# 动作文件常见存放子目录（按优先级尝试；都没有则递归兜底）
_MOTION_SUBDIRS = ("motions", "motion", "animations", "animation")


def _motion_base_name(path: str) -> str:
    """去掉动作文件后缀得到可读名（wave.motion3.json → wave）。

    注意：os.path.splitext 只去最后一个后缀，`wave.motion3.json` 会得到
    `wave.motion3`——必须显式循环去掉 .motion3.json / .motion3 / .json。
    """
    name = os.path.basename(path)
    lower = name.lower()
    for suffix in (".motion3.json", ".motion3", ".json"):
        if lower.endswith(suffix):
            name = name[: -len(suffix)]
            break
    return name


def _scan_motion_files(model3_path: str) -> List[str]:
    """扫描模型目录下的动作文件（.motion3.json，排序保证结果稳定）。

    优先常见动作子目录（motions/motion/animations/animation，取第一个
    有结果者）；这些子目录都不存在时递归兜底扫整个模型目录。
    """
    base = os.path.dirname(os.path.abspath(model3_path))
    if not os.path.isdir(base):
        return []
    found: List[str] = []
    for sub in _MOTION_SUBDIRS:
        d = os.path.join(base, sub)
        if not os.path.isdir(d):
            continue
        for name in os.listdir(d):
            if name.lower().endswith(".motion3.json"):
                found.append(os.path.join(d, name))
        if found:
            break
    if not found:
        for root, _dirs, files in os.walk(base):
            for name in files:
                if name.lower().endswith(".motion3.json"):
                    found.append(os.path.join(root, name))
    return sorted(found)


def _read_motion_duration(path: str) -> Optional[float]:
    """读 motion3.json 的 Meta.Duration（秒）；解析失败返回 None。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        dur = (data.get("Meta") or {}).get("Duration")
        return float(dur) if dur else None
    except Exception:
        return None
