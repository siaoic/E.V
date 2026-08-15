"""外部不可信文本防御：剥离 prompt-injection 标签与控制字符。

所有从工具（bing_search / weather / MCP stdio / load_skill 等）回灌到 LLM
的内容都必须先过本工具：攻击者（或被污染的搜索结果）可在网页里塞入
"忽略以上指令..."或 <system>...</system> 标签污染模型上下文。

行为：
  1. 移除常见 prompt-injection 标签（<system>/<tool_call>/<instruction> 等）
  2. 移除 ASCII 控制字符（保留 \n \t）
  3. 截断到硬上限，避免单条结果压爆 context

业务行为（输入输出格式）100% 不变：仅做"不可信内容"标注 + 净化。
"""

from __future__ import annotations

import re

# 模型偶发输出 / 搜索结果中可能出现 <system> 之类的控制标签
_TAG_INJECTION_RE = re.compile(
    r"<\s*(system|tool_call|/system|/tool_call|instruction|/instruction)\b[^>]*>",
    re.IGNORECASE,
)

# ASCII 控制字符（保留 \n 和 \t）
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b-\x0c\x0e-\x1f\x7f]")

# 搜索/网页结果单条硬上限
_MAX_RESULT_CHARS = 1500


def sanitize_external(text: str) -> str:
    """净化外部不可信文本：移除注入标签 + 控制字符 + 截断到上限。

    关键：保留 \\n \\t（爬虫换行/缩进是有意义的内容），仅清掉真控制字符。
    业务方拿到的字符串长度 ≤ _MAX_RESULT_CHARS。
    """
    if not text:
        return text
    # 1) 移除常见 prompt-injection 标签
    text = _TAG_INJECTION_RE.sub("", text)
    # 2) 移除 ASCII 控制字符（保留 \n \t）
    text = _CONTROL_CHAR_RE.sub("", text)
    # 3) 截断，避免单条结果压爆 context
    return text[:_MAX_RESULT_CHARS]
