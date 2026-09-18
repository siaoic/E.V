"""直播文本过滤：剔除 emoji 与颜文字。

直播弹幕是纯文本通道，emoji 与颜文字既无法正常展示，也会被 TTS 逐字朗读出来，
因此在生成回复文本后统一剔除。
"""

import re

# emoji 字符集合：覆盖表情/象形/交通等主要区块、杂项符号与装饰符、变体选择符与零宽连接符。
# 刻意不包含箭头区（U+2190-U+21FF 等），避免误删“→”这类正常文本符号。
_EMOJI_PATTERN = re.compile(
    "["
    "\U0001f000-\U0001faff"  # 表情、象形、交通、补充符号与扩展 A（含国旗、键帽字母）
    "\U00002600-\U000027bf"  # 杂项符号与装饰符
    "\U00002b00-\U00002bff"  # 杂项符号与箭头（⭐ ⬆ 等）
    "\U0000fe00-\U0000fe0f"  # 变体选择符
    "\U0000200d"  # 零宽连接符
    "\U0000203c\U00002049\U000020e3"  # 双感叹号、感叹问号、组合键帽
    "\U00002122\U00002139"  # 字母式符号（™ ℹ 等）
    "\U0000231a-\U0000231b\U00002328\U000023cf\U000023e9-\U000023f3\U000023f8-\U000023fa"
    "\U000024c2\U000025aa-\U000025ab\U000025b6\U000025c0\U000025fb-\U000025fe"
    "\U00003030\U0000303d\U00003297\U00003299"
    "]+",
    flags=re.UNICODE,
)

# 颜文字判定的“正文”字符：汉字、假名、谚文与 ASCII 字母数字；括号内出现这些字符就不视为颜文字。
_KAOMOJI_TEXT_CHARS = r"\u4e00-\u9fff\u3400-\u4dbf\u3040-\u30ff\uac00-\ud7afA-Za-z0-9"

# 颜文字：括号包裹的纯符号组合（如 (´▽｀)、(๑•̀ㅂ•́)و），或 >_< 这类符号夹符号写法。
_KAOMOJI_PATTERN = re.compile(
    r"[（(【\[〔｛{][^" + _KAOMOJI_TEXT_CHARS + r"\s]{1,12}[）)】\]〕｝}]"
    r"|[>＞][_＿\-=＝]{1,3}[<＜]",
    flags=re.UNICODE,
)


def strip_live_visual_symbols(text: str) -> str:
    """剔除文本中的 emoji 与颜文字，并清理因此产生的多余空白。"""

    if not text:
        return text
    cleaned = _EMOJI_PATTERN.sub("", text)
    cleaned = _KAOMOJI_PATTERN.sub("", cleaned)
    # 删除后可能留下连续空格或行首尾空格，这里统一压缩，避免弹幕出现空洞。
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    return cleaned.strip()
