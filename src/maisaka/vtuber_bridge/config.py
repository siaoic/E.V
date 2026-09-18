# -*- coding: utf-8 -*-
"""``[vtuber]`` 配置读取与注入方互斥检查。

配置与将来的 VTuber 引擎（``src/vtuber/``，独立子进程）共用
``config/bilibili_live.toml`` 的同一节（实施计划 §4-D8）；
路径约定与直播语音一致：环境变量 ``LIVE_TTS_CONFIG`` 优先，
回退仓库根 ``config/bilibili_live.toml``。

互斥硬约束（实施计划 §5）：``enabled``（V 引擎总开关）与
``lipsync_enabled``（W4 口型开关）不得同时为真——同一时刻只允许
一个 VTube Studio 注入方。
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any, Dict

_REPO_ROOT = Path(__file__).resolve().parents[3]

_DEFAULTS: Dict[str, Any] = {
    "enabled": False,  # V 引擎总开关（V0–V5 实施后生效）
    "lipsync_enabled": False,  # W4 口型 + 表情开关
    "engine_url": "http://127.0.0.1:8096",
    "vts_ws_url": "ws://127.0.0.1:8001",
    "vts_plugin_name": "MaiBot Live",
    "vts_plugin_developer": "MaiBot",
    "vts_auth_token": "",  # 首次授权成功后由服务自动写回
    "live2d_dir": "",
    "model_profile": "",
    "overlay_port": 7792,
    "mouth_param": "MouthOpen",
    "mouth_gain": 8.0,  # RMS → 口型开合度的增益
    "mouth_max": 1.0,  # 口型注入值上限（VTS 参数通常为 0..1）
    "inject_hz": 30,  # 口型注入节拍
    "emote_hold_seconds": 3.0,  # 表情默认保持时长
    "expressions": {},  # 中文名 → VTS 表情文件名（不含 .exp3.json 也可）
}


def live_config_path() -> Path:
    """定位 bilibili_live 配置文件（与 live_player 的约定一致）。"""
    configured = os.environ.get("LIVE_TTS_CONFIG", "")
    if configured:
        return Path(configured)
    return _REPO_ROOT / "config" / "bilibili_live.toml"


def load_vtuber_config() -> Dict[str, Any]:
    """读取 ``[vtuber]`` 节并归一到默认值；文件缺失等价于全部关闭。"""
    path = live_config_path()
    section: Dict[str, Any] = {}
    if path.exists():
        try:
            with open(path, "rb") as f:
                data = tomllib.load(f)
            raw = data.get("vtuber")
            if isinstance(raw, dict):
                section = raw
        except Exception:
            # 配置文件损坏时按「全部关闭」处理：口型是表演增强，不该因此拖垮直播主流程；
            # 由调用方在启动日志里如实说明。
            section = {}

    merged = dict(_DEFAULTS)
    for key, value in section.items():
        merged[key] = value
    return merged


def check_injection_exclusivity(config: Dict[str, Any]) -> None:
    """校验唯一注入方约束。

    Raises:
        RuntimeError: V 引擎与 W4 口型同时启用时抛出。
    """
    if bool(config.get("enabled")) and bool(config.get("lipsync_enabled")):
        raise RuntimeError(
            "配置冲突：[vtuber].enabled（VTuber 引擎）与 [vtuber].lipsync_enabled（口型桥）"
            "不能同时为 true——同一时刻只允许一个 VTube Studio 注入方。请只保留其一。"
        )


def write_auth_token(token: str) -> bool:
    """把 VTS 授权 token 写回配置文件；成功返回 True。

    只改 ``[vtuber].vts_auth_token`` 一个键，其余内容原样保留；
    写失败时返回 False（调用方记日志，不影响运行——token 只在内存里，
    下次重启需要重新授权）。
    """
    path = live_config_path()
    try:
        if not path.exists():
            return False
        text = path.read_text(encoding="utf-8")
        line = f'vts_auth_token = "{token}"'
        if "vts_auth_token" in text:
            import re

            text = re.sub(r'vts_auth_token\s*=\s*"[^"]*"', line, text, count=1)
        elif "[vtuber]" in text:
            # 在既有 [vtuber] 节首插入一行
            index = text.index("[vtuber]") + len("[vtuber]")
            text = text[:index] + "\n" + line + text[index:]
        else:
            # 没有 [vtuber] 节时在文件末尾新建
            text = text.rstrip() + f"\n\n[vtuber]\n{line}\n"
        path.write_text(text, encoding="utf-8")
        return True
    except Exception:
        return False
