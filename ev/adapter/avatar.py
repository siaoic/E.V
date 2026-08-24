"""数字人 / 形象适配器抽象基类：形象控制的标准契约。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Callable, Dict

from ev.adapter.base import BaseAdapter


class BaseAvatarAdapter(BaseAdapter):
    """数字人形象适配器。

    本项目实现：src/vts/controller.py 的 VTSController（VTube Studio
    WebSocket 客户端）。切换形象后端只需新增实现类，上层调用不变。
    """

    name: str = "avatar"

    @abstractmethod
    async def connect(self) -> bool:
        """连接并认证；成功返回 True，失败返回 False。"""

    @abstractmethod
    async def ensure_connected(self) -> bool:
        """断线自动重连；连接就绪返回 True。"""

    @abstractmethod
    async def close(self) -> None:
        """断开连接。"""

    @abstractmethod
    def on_event(self, event_name: str, handler: Callable) -> None:
        """订阅后端事件（如模型加载完成），handler 收消息 dict。"""

    @abstractmethod
    async def subscribe_event(self, event_name: str) -> bool:
        """向后端订阅事件；成功返回 True。"""

    @abstractmethod
    async def inject_parameters(self, params: Dict[str, float]) -> None:
        """批量注入参数（口型 / 情绪等）。"""

    @abstractmethod
    async def trigger_motion(self, motion_file: str) -> bool:
        """播放动作文件；成功返回 True。"""

    @abstractmethod
    async def trigger_hotkey(self, hotkey_id: str,
                             priority: str = "High") -> None:
        """触发热键（动作 / 表情 / 状态切换）。"""

    @abstractmethod
    async def activate_expression(self, expr_file: str,
                                  active: bool = True) -> bool:
        """切换表情；成功返回 True。"""
