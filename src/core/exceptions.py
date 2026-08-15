"""全局统一异常体系：错误码枚举 + 项目基础异常。

设计约束（贴合本项目规范）：
  - EVBaseException 的消息文本与原始异常保持一致，不追加前缀——
    确保任何上层捕获方看到的输出与改造前完全相同（异常行为 100% 不变）；
  - 错误码通过 .code 属性携带，供上层统一日志 / 统一错误事件推送；
  - 新模块接错码时仅需改 raise 类型，消息文本原样保留。

错误码分段：
  0     成功
  1xxx  外部服务连接 / 调用异常（LLM / TTS / VTS / MCP）
  4xxx  参数 / 输入错误
  5xxx  未分类内部错误
"""

from __future__ import annotations

from enum import IntEnum


class ErrorCode(IntEnum):
    """项目统一错误码。"""

    SUCCESS = 0

    # ---- 1xxx：外部服务异常 ----
    LLM_CONNECT_FAILED = 1001       # LLM 连接失败
    LLM_QUOTA_EXHAUSTED = 1002      # LLM 配额 / 限流
    TTS_SERVICE_ERROR = 1003        # TTS 服务异常
    TTS_TIMEOUT = 1004              # TTS 合成超时
    AVATAR_CONNECTION_LOST = 1005   # VTS / 桌宠连接断开
    MCP_SERVER_FAILED = 1006        # MCP 服务器启动 / 连接失败
    MCP_TOOL_FAILED = 1007          # MCP 工具调用失败

    # ---- 4xxx：参数 / 输入错误 ----
    INVALID_EVENT_DATA = 4001       # 事件 / 消息数据不合法
    TOOL_NOT_FOUND = 4002           # 工具不存在

    # ---- 5xxx：内部错误 ----
    INTERNAL_ERROR = 5000           # 未分类内部错误


class EVBaseException(Exception):
    """项目基础异常：携带错误码，消息文本保持原样。

    Args:
        code: 错误码（ErrorCode 枚举）
        msg:  异常消息（与原始异常文本一致，不加前缀）
    """

    def __init__(self, code: ErrorCode, msg: str) -> None:
        self.code = code
        self.msg = msg
        super().__init__(msg)
