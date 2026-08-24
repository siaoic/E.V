"""按句切分公共模块（3.14 SentenceChunker）：剥 <think>/<SILENT> 标记 + 短句合并。

对标 hermes `tools/tts_streaming.py` 的 SentenceChunker，但切句规则复用本项目
`src/llm/cleaners/sentence.py`（中文句末符号 。！？!?\n… + 后跟空格的英文句号），
供 TTS 管道 / 主动发言等需要"整段文本 → 可合成句子"的消费端统一使用，
消除各处重复的按句切分逻辑。
"""

import re
from typing import List

from ev.llm.cleaners.sentence import _find_sentence_end_from

# <think>...</think> 思考块：模型偶尔会把思考过程写进 content，需整体剥除
# （可能跨多个增量 delta 才闭合，feed 内先剥已闭合块，未闭合时等待）
_THINK_BLOCK_RE = re.compile(r"<think[\s>].*?</think>", flags=re.DOTALL)

# 沉默标记：主动决策遗留的 <SILENT>（proactive prompt 要求模型沉默时输出），
# 不应被合成播出。只剥尖括号包裹的明确标记形态，不剥裸词 "SILENT"，
# 避免误伤英文回复中的 silent 等正常词汇。
_SILENT_MARK_RE = re.compile(r"<SILENT>", flags=re.IGNORECASE)

# 短句合并阈值：不足该长度的句子并入下一句，避免 "哈！" 这类碎片被单切成
# 极短音频（对齐 Hermes min_len=20 的语义）
_DEFAULT_MIN_LEN = 20


class SentenceChunker:
    """增量句子切割器：吸收 LLM token 增量，产出可立即合成播报的整句。

    - 剥除 <think> 块（即使跨 delta 分裂）与 <SILENT> 沉默标记；
    - 按本项目句末符号规则切分；
    - 短于 min_len 的碎片并入后续句子（"哈！" 搭着后面一句一起播）。
    """

    def __init__(self, min_len: int = _DEFAULT_MIN_LEN) -> None:
        self.min_len = min_len
        self.buf = ""

    def feed(self, delta: str) -> List[str]:
        """吸收一段增量，返回当前已可播报的完整句子列表（无则空列表）。"""
        self.buf = _THINK_BLOCK_RE.sub("", self.buf + delta)
        if "<think" in self.buf and "</think>" not in self.buf:
            return []  # think 块未闭合，闭合标签可能在下个 delta 到达
        self.buf = _SILENT_MARK_RE.sub("", self.buf)
        out: List[str] = []
        start = 0  # 跳过会让句首过短的边界，短句并入下一句
        while True:
            idx = _find_sentence_end_from(self.buf, start)
            if idx < 0:
                break
            end = idx + 1
            head = self.buf[:end]
            if len(head.strip()) < self.min_len:
                start = end  # 太短：继续往后找边界，让碎片并入后续句子
                continue
            out.append(head.strip())
            self.buf = self.buf[end:]
            start = 0
        return out

    def flush(self) -> List[str]:
        """排空尾部（文本结束 / 长时间空闲时调用）：返回剩余可播报内容。"""
        tail = _SILENT_MARK_RE.sub("", self.buf).strip()
        self.buf = ""
        return [tail] if tail else []


def chunk_text(text: str, min_len: int = _DEFAULT_MIN_LEN) -> List[str]:
    """一次性切分整段文本为句子列表（feed + flush 的便捷封装）。"""
    chunker = SentenceChunker(min_len=min_len)
    out = chunker.feed(text)
    out.extend(chunker.flush())
    return out
