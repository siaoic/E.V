# -*- coding: utf-8 -*-
"""VTuber 桥接层的只读运行状态。

单一数据源：W6 WebUI 的 VTS 状态卡片、表情工具的可用性判断、
启动自检日志都从这里取快照。未连接/未启用时如实呈现，不伪造在线。
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any, Dict, List, Optional

_lock = threading.Lock()

_enabled = False
_connected = False
_vts_ws_url = ""
_mouth_param = ""
_mouth_value = 0.0
_last_inject_at = 0.0
_last_error = ""
_started_at = 0.0
_recent_emotes: deque = deque(maxlen=20)


def mark_enabled(enabled: bool, *, vts_ws_url: str = "", mouth_param: str = "") -> None:
    """记录服务启用状态与关键配置（启动/停止时调用）。"""
    global _enabled, _vts_ws_url, _mouth_param, _started_at
    with _lock:
        _enabled = enabled
        _vts_ws_url = vts_ws_url
        _mouth_param = mouth_param
        _started_at = time.monotonic() if enabled else 0.0
        if not enabled:
            _reset_locked()


def mark_connected(connected: bool) -> None:
    """记录 VTS 连接状态。"""
    global _connected
    with _lock:
        _connected = connected


def mark_inject(mouth_value: float) -> None:
    """记录最近一次注入的口型值。"""
    global _mouth_value, _last_inject_at
    with _lock:
        _mouth_value = float(mouth_value)
        _last_inject_at = time.monotonic()


def mark_error(error: str) -> None:
    """记录最近一次错误（空串清除）。"""
    global _last_error
    with _lock:
        _last_error = str(error or "")


def record_emote(name_zh: str, name_vts: str, duration_seconds: float) -> None:
    """记录一次表情触发（供 WebUI 展示最近表情）。"""
    with _lock:
        _recent_emotes.append(
            {
                "name": name_zh,
                "vts_name": name_vts,
                "duration_sec": round(float(duration_seconds), 2),
                "at": time.strftime("%H:%M:%S"),
            }
        )


def _reset_locked() -> None:
    global _connected, _mouth_value, _last_inject_at
    _connected = False
    _mouth_value = 0.0
    _last_inject_at = 0.0


def snapshot() -> Dict[str, Any]:
    """返回当前的只读状态快照（JSON 可序列化）。"""
    with _lock:
        uptime = time.monotonic() - _started_at if _enabled and _started_at else 0.0
        return {
            "mode": "lipsync" if _enabled else "off",
            "enabled": _enabled,
            "connected": _connected,
            "vts_ws_url": _vts_ws_url,
            "mouth_param": _mouth_param,
            "mouth_value": round(_mouth_value, 4),
            "last_inject_at": _last_inject_at,
            "uptime_sec": round(uptime, 1),
            "last_error": _last_error,
            "recent_emotes": list(_recent_emotes),
        }


def recent_emotes() -> List[Dict[str, Any]]:
    """最近表情记录副本。"""
    with _lock:
        return list(_recent_emotes)


def is_connected() -> bool:
    """VTS 当前是否已连接。"""
    with _lock:
        return _connected


def last_error() -> Optional[str]:
    """最近一次错误文本；无错误时为空串。"""
    with _lock:
        return _last_error or ""
