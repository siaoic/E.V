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


def _balanced_json(text: str, start: int) -> Optional[str]:
    """从 text[start]（应为 '{'）开始提取括号配平的 JSON 对象字符串。

    考虑字符串内的引号转义；未配平/超出 4000 字符返回 None
    （思维链正文里的 JSON 通常很短，4000 上限防病态长文本拖慢）。
    """
    depth = 0
    in_str = False
    esc = False
    for i in range(start, min(len(text), start + 4000)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


# 从正文恢复参数时认可的"参数样键"——不含这些键的 JSON 对象不采信
#（避免把思维链里举例说明的无关 JSON 当成参数）
_ARG_LIKE_KEYS = ("path", "file", "filepath", "file_path", "score",
                  "name", "note", "notes", "chord", "melody", "tempo",
                  "volume", "query", "url", "content", "text")

# 引号内带扩展名的资源路径（兜底：映射到 path 参数）
_QUOTED_PATH_RE = re.compile(
    r'"([^"\n]+\.(?:json|jpe?g|png|gif|bmp|webp|mid|midi|mp3|wav|txt))"',
    re.IGNORECASE)


# 保守修复白名单：\" \\ \/ \n \r \t \u 之外的转义视为误写的 Windows
# 单反斜杠路径（\b \f 故意排除——工具参数语境几乎不可能是退格/换页
# 意图，\b.score 这类路径名常见；标准解析成功的 JSON 不会进这步）
_JSON_LEGAL_ESCAPE_RE = re.compile(r'\\(?!["\\/nrtu])')


def _loose_json_loads(text: str) -> Optional[dict]:
    """宽松 JSON 解析：非法转义（Windows 单反斜杠路径）双写修复后重试。

    模型输出 {"path": "E:\\AI\\x.json"} 时常漏写成 "E:\\AI" 的单反斜杠
    形式——标准 json.loads 直接报错。修复后可救回，否则只能丢参。
    """
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except (json.JSONDecodeError, TypeError):
        pass
    try:
        data = json.loads(_JSON_LEGAL_ESCAPE_RE.sub(r"\\\\", text))
        return data if isinstance(data, dict) else None
    except (json.JSONDecodeError, TypeError):
        return None


def _recover_tool_args(content: str, tool_name: str) -> Optional[dict]:
    """从正文恢复空参数工具调用的参数（glm-5.3-flash 空参调用实测兜底）。

    背景：glm-5.3-flash 经中转时强制思考、思维链混在 content 输出，
    概率性生成 name 齐全但 arguments 为空的 tool_calls，参数实际写在
    思维链正文里（实测「I need to pass the path parameter:
    "E:\\...score.json"」连续 5 轮空参 play_score 耗尽轮次）。
    按可靠性从高到低尝试；取最后一个匹配——模型自我纠正后的
    最终意图在正文末尾。
    """
    if not content:
        return None

    # 1) 工具名后紧跟的 JSON 对象：name({...}) / name: {...} / name {...}
    for m in re.finditer(re.escape(tool_name) + r"\s*[:(]?\s*(\{)", content):
        obj = _balanced_json(content, m.start(1))
        if not obj:
            continue
        data = _loose_json_loads(obj)
        if data:
            return data

    # 2) 全文 JSON 对象从后往前：含 "arguments" 结构优先，否则须含参数样键
    for m in reversed(list(re.finditer(r"\{", content))):
        obj = _balanced_json(content, m.start())
        if not obj or len(obj) < 8:
            continue
        data = _loose_json_loads(obj)
        if not data:
            continue
        inner = data.get("arguments")
        if isinstance(inner, dict) and inner:
            return inner
        if any(k in data for k in _ARG_LIKE_KEYS):
            return data

    # 3) 引号内带扩展名的资源路径 → path 参数（本例最终命中处）
    paths = _QUOTED_PATH_RE.findall(content)
    if paths:
        return {"path": paths[-1]}
    return None
