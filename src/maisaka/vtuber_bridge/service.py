# -*- coding: utf-8 -*-
"""VTuber 桥接服务：把 VTS 客户端、口型驱动与表情服务组装成一个 AsyncTask。

挂载点：``src/main.py`` 的 ``_init_components()`` 里经
``async_task_manager.add_task(VtuberBridgeService())`` 启动；
``[vtuber].lipsync_enabled = false``（默认）时 ``run()`` 直接返回，
不产生任何连接与线程开销。

互斥约束见 ``config.check_injection_exclusivity``：与 V 引擎
（``[vtuber].enabled``）同真时启动即报错，不静默二选一。
"""

from __future__ import annotations

import asyncio
from typing import Optional

from src.common.logger import get_logger
from src.manager.async_task_manager import AsyncTask
from src.maisaka.tts.live_player import live_voice_player

from . import config, state
from .emote import EmoteService, EmoteToolProvider
from .lipsync import LipSyncDriver
from .vts_client import VtsClient

logger = get_logger("vtuber_bridge")

_SERVICE: Optional["VtuberBridgeService"] = None


def get_emote_service() -> Optional[EmoteService]:
    """取当前表情服务实例；未启动时返回 ``None``。"""
    if _SERVICE is None:
        return None
    return _SERVICE.emote_service


def get_service() -> Optional["VtuberBridgeService"]:
    """取桥接服务单例；未启动时返回 ``None``。"""
    return _SERVICE


class VtuberBridgeService(AsyncTask):
    """W4 口型 + 表情桥的长驻任务。"""

    def __init__(self) -> None:
        super().__init__(task_name="Vtuber Bridge Service", run_interval=0)
        self._client: Optional[VtsClient] = None
        self._lipsync: Optional[LipSyncDriver] = None
        self._emote: Optional[EmoteService] = None

    @property
    def emote_service(self) -> Optional[EmoteService]:
        """当前表情服务；未启动时为 ``None``。"""
        return self._emote

    @property
    def client(self) -> Optional[VtsClient]:
        """当前 VTS 客户端；未启动时为 ``None``。"""
        return self._client

    async def run(self) -> None:
        """读取配置 → 互斥校验 → 组装组件 → 驻留直到被取消。

        Raises:
            RuntimeError: 注入方互斥冲突时抛出（启动即报错，不静默二选一）。
        """
        global _SERVICE

        settings = config.load_vtuber_config()
        if not bool(settings.get("lipsync_enabled")):
            logger.info("[vtuber] lipsync_enabled = false，口型桥不启动")
            state.mark_enabled(False)
            return

        config.check_injection_exclusivity(settings)
        _SERVICE = self
        state.mark_enabled(
            True,
            vts_ws_url=str(settings.get("vts_ws_url")),
            mouth_param=str(settings.get("mouth_param")),
        )
        logger.info(
            f"[vtuber] 口型桥启动: {settings.get('vts_ws_url')} "
            f"mouth_param={settings.get('mouth_param')} inject_hz={settings.get('inject_hz')}"
        )

        self._client = VtsClient(
            ws_url=str(settings.get("vts_ws_url") or "ws://127.0.0.1:8001"),
            plugin_name=str(settings.get("vts_plugin_name") or "MaiBot Live"),
            plugin_developer=str(settings.get("vts_plugin_developer") or "MaiBot"),
            auth_token=str(settings.get("vts_auth_token") or ""),
            on_auth_token=self._persist_auth_token,
            logger=logger,
        )
        self._lipsync = LipSyncDriver(
            client=self._client,
            mouth_param=str(settings.get("mouth_param") or "MouthOpen"),
            mouth_gain=float(settings.get("mouth_gain") or 8.0),
            mouth_max=float(settings.get("mouth_max") or 1.0),
            inject_hz=int(settings.get("inject_hz") or 30),
            logger=logger,
        )
        self._emote = EmoteService(
            client=self._client,
            expressions=dict(settings.get("expressions") or {}),
            hold_seconds=float(settings.get("emote_hold_seconds") or 3.0),
            logger=logger,
        )

        self._client.start()
        # 首次连接等一段授权弹窗时间；未就绪时按计划明确报错（ERROR 日志），
        # 但连接循环仍在后台退避重试——VTS 稍后打开时自动接上
        connected = await self._client.wait_connected(timeout=65.0)
        if not connected:
            message = (
                "[vtuber] VTube Studio 连接超时：请确认 VTS 已启动且已加载 Live2D 模型；"
                "首次连接需要在 VTS 弹窗里点「允许」"
            )
            state.mark_error(message)
            logger.error(message)
        else:
            self._validate_expressions()
            self._lipsync.attach(live_voice_player)
            self._lipsync.start()

        try:
            # 驻留直到任务被取消；断线重连由 VtsClient 的循环自行处理
            while True:
                await asyncio.sleep(3600)
        except asyncio.CancelledError:
            raise
        finally:
            await self._teardown()

    async def _teardown(self) -> None:
        """停表情、停口型、断连接；把嘴留在闭合状态。"""
        if self._emote is not None:
            try:
                await self._emote.close_all()
            except Exception:
                logger.exception("[vtuber] 关闭表情失败")
        if self._lipsync is not None:
            try:
                await self._lipsync.stop()
            except Exception:
                logger.exception("[vtuber] 停止口型注入失败")
            self._lipsync.detach(live_voice_player)
        if self._client is not None:
            await self._client.stop()
        state.mark_enabled(False)
        logger.info("[vtuber] 口型桥已停止")

    def _validate_expressions(self) -> None:
        """连接成功后校验表情配置；不匹配只报错不改行为（VTS 侧会再拦一次）。"""
        if self._emote is None or self._client is None:
            return
        mismatched = self._emote.validate_against_model(self._client.available_expressions)
        if mismatched:
            available = "、".join(self._client.available_expressions) or "（模型没有表情文件）"
            logger.error(
                f"[vtuber] 表情配置与模型不匹配: {'、'.join(mismatched)}；"
                f"模型实际表情：{available}。请修正 [vtuber].expressions"
            )
            state.mark_error(f"表情配置与模型不匹配: {'、'.join(mismatched)}")

    def _persist_auth_token(self, token: str) -> None:
        """把首次授权拿到的 token 写回配置，之后重连不再弹窗。"""
        if config.write_auth_token(token):
            logger.info("[vtuber] VTS 授权 token 已写入 bilibili_live.toml")
        else:
            logger.error(
                "[vtuber] VTS 授权 token 写入配置失败（本次运行内仍可用，重启后需重新授权）"
            )
