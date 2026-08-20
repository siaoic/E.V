"""工具调用 / 结果格式化：日志展示（控制中心紧凑块）。"""

import json

from src.llm.utils.constants import _MAX_TOOL_HISTORY_LOG, _MAX_TOOL_RESULT_LOG

# 搜索结果日志最多逐条展示的条数（控制台防刷屏；完整结果仍进工具上下文）
_MAX_SEARCH_RESULT_SHOW = 5


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


def _format_search_result(name: str, result) -> str:
    """搜索类工具结果的友好日志：逐条 标题/链接/摘要，比整段 JSON 直观。

    仅当结果形如 {"results": [...]}（bing-cn-mcp 等搜索工具返回）时使用；
    其他结构回退 _format_tool_result。截断只影响日志展示，不裁剪进上下文。
    """
    if not isinstance(result, dict) or not isinstance(result.get("results"), list):
        return _format_tool_result(name, result)
    items = result["results"]
    if not items:
        return f"  ↳ 「{name}」搜索无结果"
    lines = [f"「{name}」搜索到 {len(items)} 条："]
    for i, item in enumerate(items[:_MAX_SEARCH_RESULT_SHOW], 1):
        title = str(item.get("title") or "（无标题）")[:80]
        url = str(item.get("url") or item.get("displayUrl") or "")[:100]
        snippet = str(item.get("snippet") or "").strip()[:120]
        lines.append(f"  {i}. {title}")
        lines.append(f"     {url}")
        if snippet:
            lines.append(f"     {snippet}")
    if len(items) > _MAX_SEARCH_RESULT_SHOW:
        lines.append(f"  …（共 {len(items)} 条，仅显示前 {_MAX_SEARCH_RESULT_SHOW} 条）")
    return "\n".join(lines)
