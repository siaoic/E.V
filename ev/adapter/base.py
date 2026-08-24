"""适配器公共基类。"""

from __future__ import annotations

from abc import ABC


class BaseAdapter(ABC):
    """外部服务适配器公共基类：统一命名与辨识。"""

    #: 适配器名称（日志 / 统计 / 切换识别用）
    name: str = "base"

    def display_name(self) -> str:
        """人类可读名称（默认即 name）。"""
        return self.name
