"""PvZ 输入注入：窗口定位 / 前台校验 / pydirectinput 点击。

硬约束（实施计划 W2）：游戏窗口未聚焦时**不执行**，回传失败事件——
DirectInput 点击落进别的窗口会是真实误操作，绝不静默改道。
"""

from __future__ import annotations

import time
from typing import List, Optional, Tuple

from .vision import VisionError

try:
    import win32con
    import win32gui
    import win32process
except ImportError as exc:  # pragma: no cover - 依赖由插件 manifest 安装
    raise ImportError("world-pvz 需要 pywin32（由插件依赖通道安装）") from exc

try:
    import pydirectinput
except ImportError as exc:  # pragma: no cover
    raise ImportError("world-pvz 需要 pydirectinput（由插件依赖通道安装）") from exc

# pydirectinput 默认每次点击有 0.1s 停顿且会打印日志，按需收紧
pydirectinput.PAUSE = 0.05
pydirectinput.FAILSAFE = True


class WindowNotFound(VisionError):
    """找不到标题匹配的游戏窗口。"""


class WindowNotFocused(VisionError):
    """游戏窗口不在前台：拒绝注入。"""


def find_window(window_title: str) -> int:
    """按标题精确匹配游戏主窗口。

    Raises:
        WindowNotFound: 找不到窗口时抛出。
    """

    hwnds: List[int] = []

    def enum_handler(hwnd, _):
        if win32gui.IsWindowVisible(hwnd) and win32gui.GetWindowText(hwnd) == window_title:
            hwnds.append(hwnd)

    win32gui.EnumWindows(enum_handler, None)
    if not hwnds:
        raise WindowNotFound(f"找不到标题为「{window_title}」的游戏窗口，请先启动游戏")
    return hwnds[0]


def is_foreground(hwnd: int) -> bool:
    """目标窗口当前是否位于前台。"""
    return win32gui.GetForegroundWindow() == hwnd


def ensure_foreground(hwnd: int, window_title: str) -> None:
    """确保游戏窗口在前台，否则抛 :class:`WindowNotFocused`。"""
    if not is_foreground(hwnd):
        raise WindowNotFocused(
            f"游戏窗口「{window_title}」不在前台，拒绝注入鼠标操作。"
            "请把游戏窗口带到前台后重试（这是防误操作的保护，不是故障）"
        )


def window_rect(hwnd: int) -> Tuple[int, int, int, int]:
    """取窗口客户区在屏幕上的 (left, top, width, height)。"""
    left, top, right, bottom = win32gui.GetClientRect(hwnd)
    origin_left, origin_top = win32gui.ClientToScreen(hwnd, (left, top))
    return origin_left, origin_top, right - left, bottom - top


class PvzInput:
    """把基准坐标系下的游戏动作注入到实际窗口。"""

    def __init__(self, *, window_title: str, base_resolution: Tuple[int, int]) -> None:
        """构建注入器。

        坐标换算：动作坐标按基准分辨率（与模板/区域同一坐标系）给出，
        运行时按客户区实际尺寸等比缩放。
        """

        self._window_title = window_title
        self._base_w, self._base_h = base_resolution
        self._hwnd: Optional[int] = None

    @property
    def hwnd(self) -> Optional[int]:
        return self._hwnd

    def reconnect(self) -> int:
        """（重新）定位游戏窗口。"""
        self._hwnd = find_window(self._window_title)
        return self._hwnd

    def _client_scale(self) -> Tuple[int, int, float, float]:
        if self._hwnd is None:
            raise WindowNotFound("尚未定位游戏窗口")
        left, top, width, height = window_rect(self._hwnd)
        if width <= 0 or height <= 0:
            raise VisionError("游戏窗口客户区尺寸异常（可能已最小化）")
        return left, top, width / self._base_w, height / self._base_h

    def _to_screen(self, base_x: float, base_y: float) -> Tuple[int, int]:
        left, top, scale_x, scale_y = self._client_scale()
        return int(left + base_x * scale_x), int(top + base_y * scale_y)

    def click_base(self, base_x: float, base_y: float, *, verify_focus: bool = True) -> None:
        """在基准坐标处点击一次。"""
        if verify_focus:
            ensure_foreground(self._hwnd or 0, self._window_title)
        screen_x, screen_y = self._to_screen(base_x, base_y)
        pydirectinput.click(screen_x, screen_y)
        time.sleep(0.05)
