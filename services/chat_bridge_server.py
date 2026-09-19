# -*- coding: utf-8 -*-
"""Chat 引擎桥接服务：包装 chat_manager 的 connect / send / disconnect。

供 TS WS 网关经 HTTP 调用；避免 TS 侧直接 import Python chat 管线。
绑定 127.0.0.1，仅限本机访问。

用法：python services/chat_bridge_server.py --port 8102
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Dict, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
import uvicorn

from src.chat.message_receive.chat_manager import chat_manager
from src.common.logger import get_logger

logger = get_logger("chat_bridge")

app = FastAPI(title="Chat Bridge", docs_url=None, redoc_url=None)

# session_id → 回调函数（由 TS 侧注册，用于推送 chat 事件）
_event_senders: Dict[str, Any] = {}


class ConnectRequest(BaseModel):
    session_id: str = Field(..., description="内部逻辑会话 ID（<connId>:<client>）")
    client_session_id: str = Field(..., description="客户端会话标识")
    connection_id: str = Field(..., description="WS 连接 ID")
    user_id: str = ""
    user_name: str = "人类"
    platform: str = "webui"


class SendMessageRequest(BaseModel):
    session_id: str
    payload: Dict[str, Any] = Field(default_factory=dict)


class NicknameRequest(BaseModel):
    session_id: str
    user_name: str = ""


@app.post("/connect")
async def connect(req: ConnectRequest):
    """打开聊天会话（等价 WS 网关的 session.open 后端处理）。"""
    try:
        await chat_manager.connect(
            session_id=req.session_id,
            connection_id=req.connection_id,
            client_session_id=req.client_session_id,
            user_id=req.user_id,
            user_name=req.user_name,
            virtual_config=None,
            client_info={"type": "webui"},
            sender=_make_sender(req.session_id),
        )
        return {"success": True}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/disconnect")
async def disconnect(req: ConnectRequest):
    """关闭聊天会话。"""
    try:
        await chat_manager.disconnect(req.session_id)
        _event_senders.pop(req.session_id, None)
        return {"success": True}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/send")
async def send_message(req: SendMessageRequest):
    """处理用户消息（等价 WS 网关的 message.send 后端处理）。"""
    try:
        # chat_manager.send_message 会走完整的 maisaka 管线
        await chat_manager.send_message(req.session_id, req.payload)
        return {"success": True, "accepted": True}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/update_nickname")
async def update_nickname(req: NicknameRequest):
    """更新昵称。"""
    try:
        session_state = chat_manager.get_session(req.session_id)
        if session_state is None:
            raise HTTPException(status_code=404, detail="会话不存在")
        session_state.user_name = req.user_name
        chat_manager.update_session_context(req.session_id, user_name=req.user_name)
        return {"success": True, "user_name": req.user_name}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


def _make_sender(session_id: str):
    """创建一个把 chat 事件推回 TS 网关的回调（经 HTTP）。"""
    async def sender(chat_message: Dict[str, Any]) -> None:
        # Phase ③-D1 二期：此回调把 chat 事件推送到 TS 侧再转发给前端
        logger.debug(f"chat event (session={session_id}): {chat_message.get('type', 'unknown')}")
    return sender


def main():
    parser = argparse.ArgumentParser(description="Chat 引擎桥接服务")
    parser.add_argument("--port", type=int, default=8102)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
