"""统一消息 Schema：事件总线载荷的强类型契约（Pydantic v2）。

字段说明见 docs/api.md「消息结构体 Schema」一节。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict

from pydantic import BaseModel, Field

# 消息结构版本：结构发生不兼容变更时 +1，消费方据此做兼容分支
API_VERSION = 1


class InputEvent(BaseModel):
    """一条进入内核的输入消息（键盘 / 语音 / 弹幕）。

    source：'text'（键盘）| 'voice'（语音识别）| 'barrage'（弹幕）| 'command'（命令）。
    """

    api_version: int = API_VERSION
    source: str
    content: str
    sender: str = "user"  # 发送者：用户 / 观众昵称
    timestamp: datetime = Field(default_factory=datetime.now)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class LLMResponse(BaseModel):
    """AI 产出的一段回复文本（流式逐句广播）。

    sender：当前播报方（'user' | 'danmaku' | 'proactive' | 'command' | 'mindcraft'）。
    """

    api_version: int = API_VERSION
    text: str
    done: bool = False  # 是否全文最后一段（结尾清字幕 / 收尾用）
    sender: str = "user"


class SpeakingEvent(BaseModel):
    """一次播报开始 / 结束。"""

    api_version: int = API_VERSION
    sender: str
    kind: str = "speech"  # 'speech' | 'command' | 'mindcraft'


class ErrorEvent(BaseModel):
    """统一错误事件（对齐 src/core/exceptions.py 的 ErrorCode）。"""

    api_version: int = API_VERSION
    code: int
    code_name: str = ""
    msg: str
    timestamp: datetime = Field(default_factory=datetime.now)


class StateChangeEvent(BaseModel):
    """全局状态机变化（idle / user_talking / ai_speaking / agent_thinking / agent_running）。"""

    api_version: int = API_VERSION
    state: str
    previous: str = ""


class SessionEndEvent(BaseModel):
    """会话结束（退出归档完成）。"""

    api_version: int = API_VERSION
    turns: int = 0
    summary: str = ""
