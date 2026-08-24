"""LLM 输出 JSON 统一容错解析工具。

集中各模块分散的 JSON 解析逻辑（agent / skill_eval / prompt_evo / evolution /
memory.lifecycle），统一以下容错手段：
- 剥 markdown 围栏（```json ... ```）；
- 全角标点/数字转半角（模型输出 JSON 时常见全角冒号/引号导致解析失败）；
- 直接解析失败后截取首个 [ / { 到末尾 ] / } 兜底（混入前后缀文字也能取到）。

对正常输入行为与各模块原实现完全一致，仅坏输入处理更宽容。
"""

from __future__ import annotations

import json
import re

# markdown 围栏（可选开头/结尾 ``` 或 ```json）
_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)

# 全角字符 → 半角（模型输出 JSON 时常见全角冒号/引号/括号导致解析失败）
_FULLWIDTH_MAP = str.maketrans(
    {
        "，": ",",
        "：": ":",
        "；": ";",
        "？": "?",
        "！": "!",
        "。": ".",
        "“": '"',
        "”": '"',
        "‘": "'",
        "’": "'",
        "（": "(",
        "）": ")",
        "【": "[",
        "】": "]",
        "　": " ",
    }
    | {chr(code): chr(code - 0xFEE0) for code in range(0xFF10, 0xFF1A)}
)  # 全角数字 ０-９ → 半角 0-9


def extract_json_text(text: str) -> str:
    """剥 markdown 围栏并做全角转半角，返回待解析文本。"""
    text = (text or "").strip()
    text = _FENCE_RE.sub("", text).strip()
    return text.translate(_FULLWIDTH_MAP)


def parse_json_object(content: str) -> dict:
    """容错解析 JSON 对象：直接解析失败后截取首个 { 到末尾 } 兜底。"""
    text = extract_json_text(content)
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, TypeError):
        pass
    start = text.find("{")
    if start < 0:
        return {}
    end = text.rfind("}")
    if end <= start:
        return {}
    try:
        data = json.loads(text[start : end + 1])
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def parse_json_array(content: str) -> list | None:
    """容错解析 JSON 数组：直接解析失败后截取首个 [ 到末尾 ] 兜底。"""
    text = extract_json_text(content)
    try:
        data = json.loads(text)
        return data if isinstance(data, list) else None
    except (json.JSONDecodeError, TypeError):
        pass
    start = text.find("[")
    if start < 0:
        return None
    end = text.rfind("]")
    if end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
        return data if isinstance(data, list) else None
    except (json.JSONDecodeError, TypeError):
        return None
