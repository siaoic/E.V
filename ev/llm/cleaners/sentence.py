"""句子分割：消费端按句末符号切分，供流式产出按句交 TTS 播报。"""

from typing import List

# 句子边界：遇符号立即切分。
# 中文：。！？…换行；英文：句号 '.' 仅在后跟空格/结尾时算边界（避开 '...'、小数）。
_SENTENCE_ENDS = "。！？!?\n…"


def _find_sentence_end_from(text: str, start: int) -> int:
    """返回 text 中从 start 起第一个句末符号的下标；找不到返回 -1。

    消费端增量扫描用：start 之前已确认不含句末符号（含「后跟空格的句号」），
    只扫描新增区域，避免每个 chunk 都从开头重扫整段 buffer。
    切句规则与 _find_sentence_end 完全一致：按符号立即切，
    英文句号仅在后跟空格/结尾时算边界。
    """
    for i in range(start, len(text)):
        if text[i] in _SENTENCE_ENDS:
            return i
        if text[i] == ".":
            # 英文句号：后跟空格或到结尾才算一句（'...'、小数、缩写不切）
            if i == len(text) - 1 or text[i + 1] == " ":
                return i
    return -1


# 停顿边界（逗号/顿号）：优先级低于句末标点。流式切段（llm_brain）在
# 无句末标点时按停顿点切分长句，配调用方的最短段长阈值防「啊，嗯，」被单切。
_PAUSE_ENDS = "，、,"


def _find_pause_end_from(text: str, start: int) -> int:
    """返回 text 中从 start 起第一个停顿标点（逗号/顿号）的下标；找不到返回 -1。

    流式切段的次优边界：句末标点缺失时按停顿标点切。与 _find_sentence_end_from
    不同，停顿标点可能落在更早未切完的区域，调用方通常从 0 全扫。
    """
    for i in range(start, len(text)):
        if text[i] in _PAUSE_ENDS:
            return i
    return -1


def _find_sentence_end(text: str) -> int:
    """返回 text 中第一个句末符号的下标；找不到返回 -1。

    主循环（消费端）唯一的切句规则：按符号立即切，
    英文句号仅在后跟空格/结尾时算边界。
    """
    return _find_sentence_end_from(text, 0)


def _split_sentences(text: str) -> List[str]:
    """按句末符号把文本切成句子（与主循环同一切句规则，无长度兜底）。"""
    sentences: List[str] = []
    buffer = text
    while True:
        idx = _find_sentence_end(buffer)
        if idx < 0:
            if buffer:
                sentences.append(buffer)
            break
        sentences.append(buffer[: idx + 1])
        buffer = buffer[idx + 1 :]
    return sentences
