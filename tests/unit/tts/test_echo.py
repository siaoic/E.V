"""TTS 回声防护（3.14）单测：相似度判定 / 窗口滑动 / 短片段跳过 / 最近播报窗口。"""
from ev.tts import echo


def _clear():
    echo._recent_spoken.clear()


class TestIsTtsEcho:
    def test_same_text_hits(self):
        assert echo.is_tts_echo("今天天气真不错", "今天天气真不错")

    def test_near_verbatim_hits(self):
        # 轻微识别差异（同字不同标点/语气词）仍应命中
        assert echo.is_tts_echo("今天天气真不错呢", "今天天气真不错。")

    def test_different_text_misses(self):
        assert not echo.is_tts_echo("主播你吃了没", "今天天气真不错")

    def test_fragment_of_long_reply_hits(self):
        # 打断瞬间捕获的往往只是长回复的一段碎片：窗口滑动兜底命中
        spoken = "欢迎来到直播间，今天我们要聊聊最近特别火的那款新游戏，还有主播的养成心得。"
        fragment = "聊聊最近特别火的那款新游戏"
        assert echo.is_tts_echo(fragment, spoken)

    def test_short_fragment_skipped(self):
        # 短于 MIN_FRAGMENT_LENGTH_FOR_ECHO 的插话不做窗口匹配（防单字误杀）
        spoken = "今天天气真不错，很适合出门走走"
        assert not echo.is_tts_echo("不错", spoken)

    def test_empty_input_false(self):
        assert not echo.is_tts_echo("", "任何文本")
        assert not echo.is_tts_echo("任何文本", "")


class TestRecentSpokenWindow:
    def test_remember_and_match(self):
        _clear()
        echo.remember_spoken("欢迎来到直播间")
        assert echo.is_echo_of_recent("欢迎来到直播间")
        assert not echo.is_echo_of_recent("完全不相关的内容")

    def test_empty_window_false(self):
        _clear()
        assert not echo.is_echo_of_recent("什么文本都不匹配")

    def test_blank_remember_ignored(self):
        _clear()
        echo.remember_spoken("   ")
        echo.remember_spoken("")
        assert echo.recent_spoken_texts() == []

    def test_window_caps_at_five(self):
        _clear()
        for i in range(8):
            echo.remember_spoken(f"第{i}句内容。")
        assert len(echo.recent_spoken_texts()) == 5
        # 窗口内是最新的 5 句
        assert echo.recent_spoken_texts()[0] == "第7句内容。"

    def test_threshold_parameter(self):
        _clear()
        echo.remember_spoken("今天天气真不错")
        # 内容不同（相似度 ~0.57）：严格阈值 0.99 下不命中
        assert not echo.is_echo_of_recent("今天天气很糟糕", threshold=0.99)
