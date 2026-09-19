# -*- coding: utf-8 -*-
"""TTS 播放代理：包住 live_voice_player 的播放/停止/状态/设备枚举。

对照迁移方案 §1.3.2：tts_service 本体不动；本代理暴露 HTTP 端点供 TS 触发。
设备枚举依赖 sounddevice（R7 风险——/config/tts-audio-devices 必须走本端点）。

用法：python services/tts_proxy_server.py --port 8101 [--host 127.0.0.1]
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT)) if str(REPO_ROOT) not in sys.path else None

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import uvicorn

from src.maisaka.tts.live_player import live_voice_player
from src.maisaka.tts.player import _resolve_output_device

app = FastAPI(title="TTS Playback Proxy", docs_url=None, redoc_url=None)


class SpeakRequest(BaseModel):
    text: str


@app.get("/health")
def health():
    return {"status": "ready", "enabled": live_voice_player._resolve_enabled()}


@app.post("/speak")
def speak(req: SpeakRequest):
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(live_voice_player.speak(req.text))
    finally:
        loop.close()
    return {"accepted": True}


@app.post("/stop")
def stop():
    live_voice_player.clear_queue()
    return {"success": True}


@app.get("/status")
def status():
    queue_len = live_voice_player._queue.qsize() if live_voice_player._queue else 0
    return {
        "enabled": live_voice_player._resolve_enabled(),
        "queue_len": queue_len,
    }


@app.get("/devices")
def list_devices():
    """设备枚举 + 同名消歧（R7：sounddevice 依赖，TS 无法替代）。"""
    import sounddevice as sd

    devices = []
    for index, info in enumerate(sd.query_devices()):
        if int(info["max_output_channels"]) > 0:
            devices.append({"index": index, "name": info["name"], "hostapi": sd.query_hostapis(info["hostapi"])["name"]})
    return {"devices": devices, "resolved": _resolve_device_name(sd, live_voice_player._resolve_param("output_device"))}


def _resolve_device_name(sd, device):
    """复用 player.py 的同名消歧逻辑，返回唯一索引或 None。"""
    if device is None:
        return None
    return _resolve_output_device(sd, device)


def main():
    parser = argparse.ArgumentParser(description="TTS 播放代理")
    parser.add_argument("--port", type=int, default=8101)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
