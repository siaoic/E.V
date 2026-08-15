"""工具调用 / 结果格式化：日志展示（控制中心紧凑块）。"""

import json

from src.llm.constants import _MAX_TOOL_HISTORY_LOG, _MAX_TOOL_RESULT_LOG


def _summarize_tool_content(content) -> str:
    """工具结果历史摘要化：跨轮次历史中的工具消息只保留结果摘要。

    dict/list → JSON 单行；超长截断并加提示。发送给模型时仍走
    _clean_messages_for_api 的既有长度上限，不影响工具链配对。
    """
    if isinstance(content, (dict, list)):
        try:
            text = json.dumps(content, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError):
            text = str(content)
    else:
        text = str(content or "")
    if len(text) <= _MAX_TOOL_HISTORY_LOG:
        return text
    return text[:_MAX_TOOL_HISTORY_LOG] + f"…（结果过长，已存 {_MAX_TOOL_HISTORY_LOG} 字摘要）"


def _format_tool_result(name: str, result) -> str:
    """工具执行结果的结构化日志：JSON 压缩为一行 + 截断到 JSON 边界。

    控制中心日志区据此展示「工具名 + 结果摘要」的紧凑块，便于调试；
    截断仅影响日志展示，不裁剪进入工具上下文的结果。

    截断策略：找最近的 '},' / '],' / '}' 边界，避免出现
    "{"title":...,"content":"（半截）" 的坏 JSON 让排查困惑。
    """
    if result is None:
        return f"  ↳ 「{name}」结果：（无返回）"
    if isinstance(result, (dict, list)):
        try:
            text = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError):
            text = str(result)
    else:
        text = str(result)
    text = text.strip()
    if len(text) > _MAX_TOOL_RESULT_LOG:
        cut = text[:_MAX_TOOL_RESULT_LOG]
        # 找最近的 JSON 边界（}, 或 ], 或 }）保证不会切到字段中间
        for sep in ("},", "],", "}"):
            idx = cut.rfind(sep)
            if idx > _MAX_TOOL_RESULT_LOG // 2:
                cut = cut[:idx + len(sep)]
                break
        text = cut + f"…（已截断至 {_MAX_TOOL_RESULT_LOG} 字符，完整结果保留在工具上下文）"
    return f"  ↳ 「{name}」结果：{text}"


def _format_tool_calls(tool_calls: list) -> str:
    """格式化工具调用日志（对标 llm-handler.js formatToolCalls）。"""
    lines = []
    for tc in tool_calls:
        name = tc["function"]["name"]
        try:
            args = json.loads(tc["function"]["arguments"] or "{}")
            arg_str = ", ".join(f"{k}={v}" for k, v in args.items()) or "（无参数）"
        except (json.JSONDecodeError, TypeError):
            arg_str = (tc["function"]["arguments"] or "")[:100] or "（无参数）"
        lines.append(f"AI调用了：{name} 工具 输入参数：{arg_str}")
    return "；".join(lines)
