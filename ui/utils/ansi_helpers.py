"""ANSI 控制码处理：剥离主程序 stdout 的颜色/控制码后写入日志控件。"""

import re

# 主程序 stdout 的 ANSI 颜色/控制码（\x1b[90m、\x1b[?25h 等），写入日志控件前剥离
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def strip_ansi(text: str) -> str:
    """剥离 ANSI 颜色/控制码，返回纯文本。"""
    return _ANSI_RE.sub("", text)
