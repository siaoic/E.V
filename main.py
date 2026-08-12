"""E.V AI 虚拟主播 —— 程序启动入口（仅此文件负责"启动程序"）。

业务逻辑、主循环、资源初始化全部在 src/core/application.py。
"""

# PySide6 6.11 的 shibokensupport 钩子会在每个模块导入后调用 inspect.getsource
# 检查是否使用 PySide6（PYSIDE-2029）。six 的动态伪模块 six.moves 没有真实源码，
# Python 3.12 在 repr 它时会访问 loader._path，而 six 的 _SixMetaPathImporter
# 缺少该属性导致 AttributeError（dateutil 等库导入 six.moves 时触发）。
# 提前补上 _path 属性可绕开此兼容性问题。必须在任何可能触发该钩子的
# import（PySide6 / transformers 链）之前执行，否则启动即崩。
import six
if not hasattr(six._SixMetaPathImporter, "_path"):
    six._SixMetaPathImporter._path = []

import asyncio
import os
import sys

from src.utils import config


if __name__ == "__main__":
    # 强制 UTF-8 + 行缓冲输出：经控制中心（QProcess 管道）启动时逐行实时
    # 捕获日志；否则 Python 在管道下默认块缓冲，启动日志攒满一次性显示。
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
        except Exception:
            pass

    if config.cfg.RUN_MODE == "pet":
        # 桌宠依赖（live2d-py / PySide6）装在项目内 vendor_pet/，
        # 避免污染系统环境；仅桌宠模式加载。
        _vendor = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor_pet")
        if os.path.isdir(_vendor) and _vendor not in sys.path:
            sys.path.insert(0, _vendor)
        from src.pet.pet_app import run_pet_app
        from src.core.application import run_with_cleanup
        run_pet_app(run_with_cleanup())
    else:
        try:
            from src.core.application import run_with_cleanup
            asyncio.run(run_with_cleanup())
        except KeyboardInterrupt:
            print("\n已退出。")
            sys.exit(0)
