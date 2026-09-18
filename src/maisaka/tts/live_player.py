# -*- coding: utf-8 -*-
"""B站直播间的独立语音朗读路径。

与通用本机朗读（``src.maisaka.tts.player`` 的 ``tts_player``）隔离：

- 直播聊天流的 AI 回复**不触发**通用朗读，改由本模块按
  ``config/bilibili_live.toml`` 的 ``[voice]`` 配置独立合成并播放到
  主播扬声器 / 直播间采集设备，避免同一句回复被播放两遍造成回声。
- 开关（``voice.enabled``）完全独立于 WebUI 的「朗读 AI 回复」；
  音色与输出设备优先取 ``[voice]`` 配置，未配置时回退通用朗读配置。
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

from src.maisaka.tts.player import TtsPlayer

# 直播平台名，与 world-bilibili 插件注入弹幕时使用的 platform 保持一致
LIVE_PLATFORM = "bilibili_live"

# 直播语音配置来源：config/bilibili_live.toml 的 [voice] 节
_REPO_ROOT = Path(__file__).resolve().parents[3]
_LIVE_CONFIG_PATH = Path(
    os.environ.get("LIVE_TTS_CONFIG", str(_REPO_ROOT / "config" / "bilibili_live.toml"))
)

_DEFAULT_TTS_URL = "http://127.0.0.1:8095"


def _load_live_voice_config() -> dict:
    """读取直播语音配置，缺失或解析失败时返回空 dict（等价未启用）。"""
    if not _LIVE_CONFIG_PATH.exists():
        return {}
    try:
        with open(_LIVE_CONFIG_PATH, "rb") as f:
            cfg = tomllib.load(f)
    except Exception:
        return {}
    voice = cfg.get("voice") or {}
    return voice if isinstance(voice, dict) else {}


def _as_int(config: dict, name: str, default: int) -> int:
    try:
        return int(config.get(name) or default)
    except (TypeError, ValueError):
        return default


def _live_enabled() -> bool:
    """直播语音独立开关：只读 [voice].enabled，不与通用朗读开关耦合。"""
    return bool(_load_live_voice_config().get("enabled", False))


def _live_param(name: str) -> Any:
    """直播音色/设备参数：优先取 [voice]，未配置则回退通用朗读配置，保证可用。"""
    voice = _load_live_voice_config()
    val = voice.get(name)
    if val:
        return val
    try:
        from src.config.config import global_config

        val = getattr(global_config.chat.tts_read, name, None)
        if val:
            return val
    except Exception:
        pass
    return None


def _build_live_player() -> TtsPlayer:
    voice = _load_live_voice_config()
    base_cfg = {
        "enabled": bool(voice.get("enabled", False)),
        "base_url": str(voice.get("base_url") or _DEFAULT_TTS_URL).rstrip("/"),
        "spk_audio": voice.get("spk_audio") or None,
        "prompt_audio": voice.get("prompt_audio") or None,
        "prompt_text": voice.get("prompt_text") or None,
        "stream_chunk": _as_int(voice, "stream_chunk", 25),
        "overlap_len": _as_int(voice, "overlap_len", 5),
        "sample_rate": _as_int(voice, "sample_rate", 32000),
        "output_device": voice.get("output_device") or None,
    }
    return TtsPlayer(base_cfg, meta_resolver=_live_enabled, param_resolver=_live_param)


# 直播语音播放器单例
live_voice_player = _build_live_player()

__all__ = ["LIVE_PLATFORM", "live_voice_player"]