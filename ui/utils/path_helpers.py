"""路径工具：frozen 时定位项目根 / 扫描 live2d 模型文件。"""

import glob
import os
import sys
from typing import List


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


def _list_models(project_root: str) -> List[str]:
    """扫描 live2d 文件夹下全部 .model3.json（相对项目根目录路径）。"""
    rels: List[str] = []
    base = os.path.join(project_root, "live2d")
    if not os.path.isdir(base):
        return rels
    for p in sorted(glob.glob(
            os.path.join(base, "**", "*.model3.json"), recursive=True)):
        rel = os.path.relpath(p, project_root).replace("\\", "/")
        if rel not in rels:
            rels.append(rel)
    return rels
