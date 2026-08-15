"""内容清洗：动作标注 / HTML / 思考块 / 工具调用文本 / 颜文字 / emoji。"""

import re

from tools.memory import memory

# 角色扮演动作/表情标注（*blushes deeply* 等）：LLM 常用来表达情绪/动作，
# 若不清除 TTS 会直接念出来（"星号 blushes deeply 星号"），既"瞎喊"又拖慢合成。
_ACTION_ANNOT_RE = re.compile(r"\*[^*\n]*\*")

# 模型偶发输出 <time>...</time> 之类 HTML 标签（字段名/伪标签），
# 不清理会被 TTS 直接念成"小于 time 大于"。统一替换为空。
_HTML_TAG_RE = re.compile(r"<[^<>]{1,32}>")

# 思考内容防御过滤（对标 live-2d(2) filterThinkingContent）：
# Gemini/DeepSeek 等模型偶尔把 <think>/<thinking> 块混进 content
_THINK_BLOCK_RE = re.compile(r"<(?:think|thinking)>[\s\S]*?</(?:think|thinking)>", re.IGNORECASE)

# 清洗时移除工具调用文本，防止 TTS 念出 <tool_call> 等
_TOOL_CALL_TEXT_RE = re.compile(
    r"<tool_call>[\s\S]*?</tool_call>|<\w+\s+[^>]+?\/>", re.IGNORECASE
)

# 颜文字（(´•ω•̥`) / (T_T) 等）：GPT-SoVITS 无法合成（HTTP 500 死音）。
# 只匹配含"颜文字信号字符"的短括号（假名/希腊字母 ω/´/•/组合符/点/下划线等），
# 不会误伤 "(笑)" 这类正常括号。
_KAOMOJI_RE = re.compile(
    r"[（(](?=[^（）()]*[\u3040-\u30ff\u0370-\u03ff\u00b4\u2022\u0300-\u036f"
    r"\u30fb\u00b7\u005e\u005f\u0060])[^（）()]{1,14}[)）]"
)

# Emoji 过滤：匹配主流 Unicode emoji 区域（含 ZWJ 序列、肤色修饰符等）
_EMOJI_RE = re.compile(
    "["
    "\U0001F600-\U0001F64F"   # 表情符号
    "\U0001F300-\U0001F5FF"   # 符号和象形文字
    "\U0001F680-\U0001F6FF"   # 交通和地图符号
    "\U0001F1E0-\U0001F1FF"   # 国旗
    "\U00002702-\U000027B0"   # 杂项符号
    "\U00002460-\U000024FF"   # 带括号的字母数字（Enclosed Alphanumerics，含 Ⓜ）
    "\U0001F000-\U0001F1FF"   # 麻将/多米诺/扑克牌/封闭字母数字补充
    "\U0001F900-\U0001F9FF"   # 补充符号和象形文字
    "\U0001FA00-\U0001FA6F"   # 国际象棋符号
    "\U0001FA70-\U0001FAFF"   # 符号和象形文字扩展-A
    "\U00002600-\U000026FF"   # 杂项符号
    "\U0000FE00-\U0000FE0F"   # 变体选择器
    "\U0000200D"              # 零宽连接符（ZWJ）
    "\U0000200C"              # 零宽非连接符
    "\U00002B50"              # 白色五角星⭐
    "\U00002728"              # 火花✨
    "\U00002764"              # 红心❤
    "\U0001F48B"              # 吻💋
    "\U0001F4AF"              # 100💯
    "\U0001F4A1"              # 灯泡💡
    "]+"
)


def _strip_emojis(text: str) -> str:
    """移除文本中的 Emoji 字符。"""
    return _EMOJI_RE.sub("", text) if text else text


def _filter_thinking_content(text: str) -> str:
    """过滤模型混入 content 的思考内容（对标 live-2d(2) filterThinkingContent）。"""
    if not text:
        return text
    filtered = _THINK_BLOCK_RE.sub("", text)
    if re.match(r"^思考\s*\n", filtered):
        return ""
    if re.match(r"^Thinking\s*\n", filtered, re.IGNORECASE):
        return ""
    return filtered


def _remove_tool_calls_from_content(content: str) -> str:
    """从内容中移除工具调用部分（对标 llm-client.js _removeToolCallsFromContent）。"""
    if not content:
        return content
    return _TOOL_CALL_TEXT_RE.sub("", content).strip()


def _clean_sentence(sentence: str) -> str:
    """纯文本清洗（无副作用）：记忆标签 / 动作标注 / HTML / 工具文本 / 颜文字 / emoji。

    关键：绝不 strip 首尾空白！句子按软边界（空格）切分时，
    分界空格在 chunk 尾部；按硬边界（标点）切分时在下一 chunk 头部。
    strip 会吞掉单词分界，导致英文粘连（I mean → Imean），
    送进 TTS 后合成异常慢、甚至念出 *blushes* 之类的"瞎喊"。
    """
    if not sentence:
        return sentence
    sentence = memory.extract_and_strip(sentence)
    sentence = _filter_thinking_content(sentence)
    sentence = _ACTION_ANNOT_RE.sub(" ", sentence)
    sentence = _HTML_TAG_RE.sub("", sentence)
    sentence = _TOOL_CALL_TEXT_RE.sub(" ", sentence)
    sentence = _KAOMOJI_RE.sub(" ", sentence)
    sentence = _strip_emojis(sentence)
    return sentence
