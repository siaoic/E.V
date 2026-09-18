# -*- coding: utf-8 -*-
"""VTuber 表演层桥接（W4：Live2D 口型联动 + 表情）。

- ``service.VtuberBridgeService``：长驻 AsyncTask，读 ``[vtuber]`` 配置并组装组件；
- ``lipsync.LipSyncDriver``：TTS 包络 → 口型曲线 → 30Hz VTS 注入；
- ``emote``：``vtuber_emote`` 表情工具（独立 ToolProvider，见实施计划 §4-D7）；
- ``state``：只读运行状态（W6 WebUI 与启动自检共用）。

与将来的 VTuber 引擎（``src/vtuber/``，V0–V5）共用 ``[vtuber]`` 配置节；
同一时刻只允许一个 VTS 注入方（§5 硬约束）。
"""

from .config import check_injection_exclusivity, load_vtuber_config
from .emote import EmoteService, EmoteToolProvider
from .lipsync import LipSyncDriver
from .service import VtuberBridgeService, get_emote_service
from .state import snapshot
from .vts_client import VtsClient

__all__ = [
    "check_injection_exclusivity",
    "load_vtuber_config",
    "EmoteService",
    "EmoteToolProvider",
    "LipSyncDriver",
    "VtuberBridgeService",
    "get_emote_service",
    "snapshot",
    "VtsClient",
]
