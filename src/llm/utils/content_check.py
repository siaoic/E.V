"""实质内容检测：句子清理后若不含任何中英文/数字/假名（纯标点、符号、空白），
直接丢弃——GPT-SoVITS 对无意义文本会合成退化，输出拖长音（"啊——"怪叫）。"""

import re

_HAS_CONTENT_RE = re.compile(r"[A-Za-z0-9\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]")


def has_content(text: str) -> bool:
    """文本是否含实质内容（至少一个中英文 / 数字 / 假名字符）。"""
    return bool(_HAS_CONTENT_RE.search(text or ""))
