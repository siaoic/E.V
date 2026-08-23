"""SentenceChunker（3.14）单测：按句切分 / think 剥离 / SILENT 剥离 / 短句合并 / flush。

切分语义对齐 hermes tts_streaming.SentenceChunker：
- 默认 min_len=20：不足长度的句子并入后续句子；
- think 块未闭合时整体暂存（闭合标签可能在下个 delta 到达），闭合后剥除再排出；
- 需要"逐句切分"的用例显式传小 min_len。
"""
from src.tts.chunker import SentenceChunker, chunk_text


class TestBasicSplit:
    def test_split_simple_sentences(self):
        out = chunk_text("你好呀。今天天气真不错！对吧？", min_len=1)
        assert out == ["你好呀。", "今天天气真不错！", "对吧？"]

    def test_newline_is_boundary(self):
        out = chunk_text("第一句。\n第二句。", min_len=1)
        assert out == ["第一句。", "第二句。"]


class TestThinkBlock:
    def test_closed_think_stripped(self):
        out = chunk_text("<think>我要想一下。</think>这是最终回答。", min_len=1)
        assert out == ["这是最终回答。"]

    def test_think_spanning_deltas(self):
        c = SentenceChunker(min_len=1)
        # think 块未闭合：整体暂存，不产出
        assert c.feed("开头。<think>中间想") == []
        # 闭合标签到达后剥除思考内容，排空暂存文本
        assert c.feed("了半天还没想好</think>") == ["开头。"]
        assert c.feed("后续内容。") == ["后续内容。"]


class TestSilentMark:
    def test_silent_tag_stripped(self):
        out = chunk_text("<SILENT> 今天先不说了。", min_len=1)
        assert out == ["今天先不说了。"]

    def test_bare_silent_word_kept(self):
        # 只剥尖括号标记，不误伤英文正常词
        out = chunk_text("please keep silent.", min_len=1)
        assert any("silent" in s for s in out)


class TestShortMerge:
    def test_short_head_merged_into_next(self):
        # "哈！" 只有 2 字，并入下一句一起播
        c = SentenceChunker(min_len=5)
        assert c.feed("哈！今天运气真好呀。") == ["哈！今天运气真好呀。"]

    def test_long_sentence_split_immediately(self):
        c = SentenceChunker(min_len=5)
        assert c.feed("这是一句足够长的句子。") == ["这是一句足够长的句子。"]

    def test_tail_shorter_than_min_still_flushed(self):
        # flush 语义：尾部不足 min_len 也照常排出（宁多勿漏）
        c = SentenceChunker(min_len=5)
        assert c.feed("长句子。") == []
        assert c.flush() == ["长句子。"]


class TestFlush:
    def test_flush_returns_tail(self):
        c = SentenceChunker()
        assert c.feed("没有标点的尾巴") == []
        assert c.flush() == ["没有标点的尾巴"]

    def test_flush_empty_returns_none(self):
        c = SentenceChunker()
        c.feed("")
        assert c.flush() == []

    def test_flush_resets_buffer(self):
        c = SentenceChunker(min_len=1)
        assert c.feed("你好") == []
        assert c.flush() == ["你好"]
        assert c.feed("世界。") == ["世界。"]
