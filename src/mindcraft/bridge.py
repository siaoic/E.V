"""Mindcraft 双向桥：socket.io 客户端连接 MindServer（对齐 live-2d 重构版）。

架构（live-2d MindServer 重构版）：
- Node 引擎（plugins/mindcraft，由控制中心插件页启停）持有 MindServer + MC bot；
- 本桥作为 socket.io 客户端连入 MindServer，实现双向对话：
  * 用户输入  → send-message 转发给指定 agent（bot 在游戏内说话/行动）；
  * bot 输出  → bot-output 事件回调 → 主播 TTS 朗读；
  * 主播朗读期间 → tts-playing 通知 bot 暂停自说自话（self_prompter）。
"""

import asyncio
import logging
from typing import Awaitable, Callable, Optional

import socketio

logger = logging.getLogger(__name__)

# bot 输出回调签名：async def cb(message: str) -> None
BotOutputCallback = Callable[[str], Awaitable[None]]
# agents-status 回调签名：async def cb(agents: list) -> None
AgentsStatusCallback = Callable[[list], Awaitable[None]]


class MindcraftBridge:
    """MindServer 的异步 socket.io 客户端（python-socketio AsyncClient）。

    引擎可能晚于主播程序启动，连接失败不抛给调用方——由
    Application._mindcraft_loop 周期重试。
    """

    def __init__(self, server_url: str, agent_name: str,
                 on_bot_output: Optional[BotOutputCallback] = None,
                 on_agents_status: Optional[AgentsStatusCallback] = None) -> None:
        self.server_url = server_url
        self.agent_name = agent_name
        self._on_bot_output = on_bot_output
        self._on_agents_status = on_agents_status
        self._connected = False
        self.sio = socketio.AsyncClient()
        self.sio.on("connect", self._handle_connect)
        self.sio.on("disconnect", self._handle_disconnect)
        self.sio.on("bot-output", self._handle_bot_output)
        self.sio.on("agents-status", self._handle_agents_status)

    @property
    def connected(self) -> bool:
        return self._connected

    async def connect(self, timeout: float = 8.0) -> None:
        """连接 MindServer 并订阅 agent 输出；失败抛异常由调用方重试。

        仅用 polling 传输：websocket 升级依赖新版 aiohttp（ClientWSTimeout），
        与项目弹幕库 blivedm 锁定的 aiohttp~=3.9.0 冲突，聊天场景 polling 足够。
        """
        await self.sio.connect(self.server_url, wait_timeout=timeout,
                               transports=["polling"])
        # connect 事件回调里已 emit listen-to-agents；此处兜底补一次
        if self.sio.connected:
            await self.sio.emit("listen-to-agents")

    async def disconnect(self) -> None:
        if self.sio.connected:
            await self.sio.disconnect()

    async def send_message(self, text: str, source: str = "VOICE_INPUT") -> None:
        """把用户输入转发给指定 agent（bot 在游戏内响应）。"""
        # tuple 数据会被 python-socketio 展开为多个参数，对应服务端
        # socket.on('send-message', (agentName, data) => ...)
        await self.sio.emit(
            "send-message", (self.agent_name, {"from": source, "message": text}))

    async def set_tts_playing(self, playing: bool) -> None:
        """通知 agent：主播正在/停止朗读，bot 据此暂停/恢复自说自话。"""
        await self.sio.emit("tts-playing", (self.agent_name, playing))

    async def _handle_connect(self) -> None:
        self._connected = True
        await self.sio.emit("listen-to-agents")

    async def _handle_disconnect(self) -> None:
        self._connected = False

    async def _handle_bot_output(self, agent_name: str, message: str) -> None:
        if agent_name != self.agent_name:
            return
        if self._on_bot_output is None:
            logger.warning("未注册 bot-output 回调，丢弃回复：%s", message)
            return
        try:
            await self._on_bot_output(message)
        except Exception:
            logger.exception("bot-output 回调出错")

    async def _handle_agents_status(self, agents: list) -> None:
        if self._on_agents_status is None:
            return
        try:
            await self._on_agents_status(agents)
        except Exception:
            logger.exception("agents-status 回调出错")
