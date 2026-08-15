"""Qwen 文本格式工具调用解析（对标 llm-client.js _parseQwenToolCalls）。"""

import json
import re
import time
from typing import List, Optional

# Qwen 文本格式工具调用（<tool_call>{json}</tool_call> 或 <fn_name attr="value"/>）
_QWEN_TOOL_CALL_JSON_RE = re.compile(r"<tool_call>\s*(\{[\s\S]*?\})\s*</tool_call>", re.IGNORECASE)
_QWEN_TOOL_CALL_XML_RE = re.compile(r"<(\w+)\s+([^>]+?)\/>", re.IGNORECASE)
# 常见 HTML 自闭合标签——XML 格式工具解析时跳过，避免误伤
_HTML_SELF_CLOSING_TAGS = {
    "br", "hr", "a", "img", "input", "meta", "link", "source", "area",
    "base", "col", "embed", "param", "track", "wbr",
}


def _parse_qwen_tool_calls(content: str) -> Optional[List[dict]]:
    """解析 Qwen 模型的文本格式工具调用（对标 llm-client.js _parseQwenToolCalls）。

    格式1：<tool_call>{"name": ..., "arguments": {...}}</tool_call>
    格式2：<function_name attr1="value1" attr2="value2"/>
    """
    if not content:
        return None

    tool_calls: List[dict] = []
    # 时间戳前缀保证跨轮次唯一（历史保留完整工具链后，id 不能与其他轮冲突）
    _ts = int(time.time() * 1000)

    # 格式1：JSON
    for m in _QWEN_TOOL_CALL_JSON_RE.finditer(content):
        try:
            data = json.loads(m.group(1))
        except (json.JSONDecodeError, TypeError):
            continue
        name = data.get("name") or ""
        arguments = data.get("arguments") or {}
        tool_calls.append({
            "id": f"call_qwen_{_ts}_{len(tool_calls)}",
            "type": "function",
            "function": {
                "name": name,
                "arguments": json.dumps(arguments, ensure_ascii=False),
            },
        })

    # 格式2：XML 属性（跳过常见 HTML 自闭合标签）
    for m in _QWEN_TOOL_CALL_XML_RE.finditer(content):
        fname = m.group(1)
        if fname.lower() in _HTML_SELF_CLOSING_TAGS:
            continue
        attrs = dict(re.findall(r'(\w+)="([^"]*)"', m.group(2)))
        tool_calls.append({
            "id": f"call_qwen_{_ts}_{len(tool_calls)}",
            "type": "function",
            "function": {
                "name": fname,
                "arguments": json.dumps(attrs, ensure_ascii=False),
            },
        })

    return tool_calls or None
