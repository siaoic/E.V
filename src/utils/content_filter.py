"""内容过滤：检测用户消息中的骂人用语，按概率只把骂人的那几个字替换为 Filter。

- `ProfanityFilter.censor(text)`：子串匹配，命中则把脏话原位替换为 `Filter`，
  其余内容原样保留（不整句过滤）；
- 返回 `(masked_text, hit)`，hit=True 表示检测到不当用语；
- 是否真正触发过滤（70%）由调用方决定（main.py 用 `random.random()` 掷骰），
  本模块只负责「检测 + 替换」，保证替换结果不含脏话。

注意：单字（操/草/滚）易误伤「操场/草莓/滚动」等正常词，不收录；
`卧槽/我靠` 等感叹词不对人，也不收录。
"""

import re
from typing import List, Optional, Tuple

# 骂人/侮辱用语（多字精确子串，大小写不敏感；长词在前避免被短词先截断）
PROFANITY_WORDS: List[str] = [
    # 人格侮辱
    "傻逼", "傻B", "傻X", "煞笔", "沙比", "撒比", "憨批", "蠢逼", "傻缺", "傻叉",
    "傻鸟", "蠢驴", "脑残", "智障", "弱智", "白痴", "蠢货", "废物", "人渣", "败类",
    "畜生", "畜牲", "杂种", "狗东西", "狗杂种", "狗日的", "贱人", "贱货", "婊子",
    "王八蛋", "王八羔子", "混蛋", "混账", "龟孙", "猪脑子", "脑子进水",
    "傻屌", "傻帽", "二逼", "二百五", "愣头青", "草包", "脓包", "饭桶", "窝囊废", "软蛋",
    "孬种", "怂包", "怂货", "低能儿", "智障儿", "脑瘫",
    "猪狗不如", "衣冠禽兽", "斯文败类", "人面兽心", "狼心狗肺", "狗腿子", "走狗",

    # 骂亲属/粗口
    "草泥马", "你妈逼", "你妈比", "你妈的", "他妈的", "去你妈的", "操你妈", "草你妈",
    "艹你妈", "操你妹", "你麻痹", "妈卖批", "麻痹", "尼玛", "妈逼",
    "你奶奶的", "你爷爷的", "你姥姥的", "你大爷的", "你二大爷的", "他奶奶的", "他爷爷的",
    "干你娘", "操你祖宗", "日你妈", "你妹的", "你妈的逼", "妈了个逼",

    # 命令辱骂
    "去死", "去死吧", "滚蛋", "滚犊子", "滚一边去", "找死", "有病", "神经病",
    "吃屎", "啃屎", "狗屎", "屎", "放屁", "屁话", "胡扯", "扯淡", "瞎说", "胡说八道",

    # 方言/网络流行
    "扑街", "冚家铲", "龟儿子", "瓜娃子", "宝器", "瘪犊子", "完犊子", "小赤佬",

    # 拼音/缩写
    "CNM", "NMSL", "WCNM", "TMD", "MMP", "WDNMD", "SB", "MD",
    "QNMLGB", "TMLGB", "WQNMLGB", "NMB", "2B", "BS",

    # 英文粗口
    "fuck", "shit", "bitch", "asshole", "dumbass",
    "motherfucker", "cunt", "dickhead", "bastard", "whore", "slut", "crap", "damn", "hell", "ass",
]

# 替换词（TTS 会直接读出这个词）
MASK_WORD = "Filter"


class ProfanityFilter:
    def __init__(self, words: Optional[List[str]] = None,
                 mask: str = MASK_WORD) -> None:
        # 长词优先，让「去你妈的」在「你妈的」之前匹配
        self._words = sorted(words or PROFANITY_WORDS, key=len, reverse=True)
        self._mask = mask
        pattern = "|".join(re.escape(word) for word in self._words)
        self._regex = re.compile(pattern, re.IGNORECASE)

    @property
    def words(self) -> List[str]:
        return list(self._words)

    def has_hit(self, text: str) -> bool:
        """是否包含骂人用语（不替换）。"""
        return self._regex.search(text) is not None

    def censor(self, text: str) -> Tuple[str, bool]:
        """把命中的脏话替换为 Filter，其余保留；返回 (替换后文本, 是否命中)。"""
        if not self._regex.search(text):
            return text, False
        return self._regex.sub(self._mask, text), True