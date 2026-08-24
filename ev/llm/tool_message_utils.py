"""工具消息序列工具 —— 严格参考 live-2d(2) 的 tool-message-utils.js。

严格模式 API（DeepSeek 等）要求：
  1. assistant 消息带 tool_calls 时，后面必须紧跟与之一一配对的 tool 响应消息
  2. tool 消息前面必须存在声明了对应 tool_call_id 的 assistant 消息

任何来源的消息序列（内存、持久化历史、裁剪结果）都可能违反上述约束，
本模块提供「序列清洗」和「不切断工具调用链的裁剪」两个能力。
"""

from __future__ import annotations

from typing import List, Optional


def _has_tool_calls(message: dict) -> bool:
    return (
        message.get("role") == "assistant"
        and isinstance(message.get("tool_calls"), list)
        and len(message["tool_calls"]) > 0
    )


def _get_tool_call_ids(tool_calls: list) -> List[str]:
    ids = []
    for tc in tool_calls or []:
        tid = tc.get("id") if isinstance(tc, dict) else None
        if isinstance(tid, str) and tid:
            ids.append(tid)
    return ids


def _clone_without_tool_calls(message: dict) -> dict:
    return {k: v for k, v in message.items() if k != "tool_calls"}


def _has_usable_assistant_content(message: dict) -> bool:
    content = message.get("content")
    if content is None:
        return False
    if isinstance(content, str):
        return content.strip() != ""
    return True


def _collect_consecutive_tool_messages(messages: list, start_index: int):
    """收集从 start_index 开始的连续 tool 消息块。"""
    tool_messages = []
    index = start_index
    while index < len(messages) and messages[index].get("role") == "tool":
        tool_messages.append(messages[index])
        index += 1
    return tool_messages, index


def _find_tool_message_by_id(tool_messages: list, tid: str, used_indexes: set) -> Optional[dict]:
    for i, msg in enumerate(tool_messages):
        if i in used_indexes:
            continue
        if msg.get("tool_call_id") == tid:
            used_indexes.add(i)
            return msg
    return None


def sanitize_tool_message_sequence(messages: list) -> list:
    """清理消息序列，保证 assistant.tool_calls 与 tool 响应严格配对。

    - 孤立的 tool 消息（前面没有声明对应 tool_call_id 的 assistant）直接丢弃
    - assistant+tool_calls 的后续 tool 响应不完整时，剥离 tool_calls 字段，
      有文本内容则保留文本，否则整条丢弃
    """
    if not isinstance(messages, list):
        return []

    sanitized: list = []
    i = 0
    while i < len(messages):
        message = messages[i]
        if not isinstance(message, dict):
            i += 1
            continue

        if message.get("role") == "tool":
            # 孤立 tool 消息，丢弃
            i += 1
            continue

        if not _has_tool_calls(message):
            sanitized.append(message)
            i += 1
            continue

        expected_ids = _get_tool_call_ids(message["tool_calls"])
        tool_messages, next_index = _collect_consecutive_tool_messages(messages, i + 1)
        used_indexes: set = set()
        matched_tools = [
            t
            for tid in expected_ids
            if (t := _find_tool_message_by_id(tool_messages, tid, used_indexes))
        ]

        if expected_ids and len(matched_tools) == len(expected_ids):
            # 工具调用链完整，按 tool_calls 声明顺序原样保留
            sanitized.append(message)
            sanitized.extend(matched_tools)
        elif _has_usable_assistant_content(message):
            # 链不完整，剥离 tool_calls 只保留文本
            sanitized.append(_clone_without_tool_calls(message))

        # 跳过已处理的连续 tool 块
        i = next_index
        continue

    return sanitized


def build_conversation_units(messages: list) -> list:
    """把消息分组为不可分割的「单元」：

    assistant+tool_calls 及其后续连续 tool 响应是一个单元，其余消息各自成单元。
    """
    units: list = []
    i = 0
    while i < len(messages):
        message = messages[i]
        if _has_tool_calls(message):
            tool_messages, next_index = _collect_consecutive_tool_messages(messages, i + 1)
            units.append([message] + tool_messages)
            i = next_index
        else:
            units.append([message])
            i += 1
    return units


def trim_messages_preserving_tool_rounds(messages: list, max_messages: int) -> list:
    """按条数裁剪，但以「单元」为最小粒度从后向前保留，保证不切断工具调用链。

    若最新的单个单元本身就超过上限，也至少完整保留这一个单元。
    """
    try:
        limit = int(max_messages)
    except (TypeError, ValueError):
        return sanitize_tool_message_sequence(messages)
    if limit < 1:
        return sanitize_tool_message_sequence(messages)

    sanitized = sanitize_tool_message_sequence(messages)
    units = build_conversation_units(sanitized)

    kept_units: list = []
    kept_count = 0
    for unit in reversed(units):
        if kept_units and kept_count + len(unit) > limit:
            break
        kept_units.insert(0, unit)
        kept_count += len(unit)

    result: list = []
    for unit in kept_units:
        result.extend(unit)
    return result
