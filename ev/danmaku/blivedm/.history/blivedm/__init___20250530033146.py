"""forward 兼容层：真实实现已搬迁至 ev.danmaku.blivedm..history.blivedm.__init___20250530033146（别名方案，monkeypatch 免疫）。"""
from __future__ import annotations
import sys as _sys

_REAL_MOD = "ev.danmaku.blivedm..history.blivedm.__init___20250530033146"
_real = __import__(_REAL_MOD, fromlist=["*"])
_sys.modules[__name__] = _real
