"""API 消息清洗：确保 OpenAI 协议兼容（对标 llm-client.js _cleanMessagesForAPI）。"""

import json
import re
from typing import List

from ev.llm.utils.constants import _MAX_TOOL_CONTENT_LENGTH
from ev.llm.tool_message_utils import sanitize_tool_message_sequence

# 控制字符（可能导致 JSON 解析失败）：移除不可见字符，保留换行符(\n)和制表符(\t)
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b-\x0c\x0e-\x1f\x7f]")


def _clean_messages_for_api(messages: List[dict]) -> List[dict]:
    """清理消息格式，确保 API 兼容（对标 llm-client.js _cleanMessagesForAPI）。

    - assistant 有 tool_calls 但 content 为 null → 设为 ''（部分 API 要求 content 非 null）
    - tool 消息：content 对象→JSON 字符串、移除控制字符、超过 8000 截断
    - system 消息全部前移到开头（严格模式 API 如 SiliconFlow/Qwen 要求 system
      必须在消息列表最前，否则报 20015 "System message must be at the beginning"；
      近因效应注入的尾部 system 段与历史中的 system 消息都在此归一化）
    - 最后用 sanitize_tool_message_sequence 兜底，保证 tool_calls 与 tool 响应严格配对
    """
    normalized: List[dict] = []
    for msg in messages:
        msg = dict(msg)
        if msg.get("role") == "assistant":
            # 有 tool_calls 但 content 为 null 时，某些 API 要求 content 不能为 null
            if msg.get("content") is None and msg.get("tool_calls"):
                msg["content"] = ""
        elif msg.get("role") == "tool":
            content = msg.get("content")
            # content 是对象/数组 → 转 JSON 字符串
            if isinstance(content, (dict, list)):
                try:
                    content = json.dumps(content, ensure_ascii=False)
                except (TypeError, ValueError):
                    content = str(content)
            # 确保 content 是字符串
            if not isinstance(content, str):
                content = str(content or "")
            # 移除控制字符（可能导致 JSON 解析失败），保留 \n 和 \t
            content = _CONTROL_CHAR_RE.sub("", content)
            # 超长内容截断，避免超大响应
            if len(content) > _MAX_TOOL_CONTENT_LENGTH:
                content = content[:_MAX_TOOL_CONTENT_LENGTH] + "...(内容过长已截断)"
            msg = {
                "role": "tool",
                "name": msg.get("name") or "unknown_tool",
                "content": content,
                "tool_call_id": msg.get("tool_call_id"),
            }
        normalized.append(msg)

    # 严格模式 API 要求 system 必须在最前：把 system 消息稳定前移（保持原有
    # 相对顺序与内容不变），user/assistant/tool 相对顺序不受影响
    normalized.sort(key=lambda m: m.get("role") != "system")

    # 最后一道防线：清理 assistant.tool_calls 与 tool 响应不配对的序列
    return sanitize_tool_message_sequence(normalized)
