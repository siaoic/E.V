"""键盘敏感词过滤：加载 data/keyboard.txt 大词库（每行一个词，7 万级）。

参考 Ikaros-521/AI-Vtuber 的 badwords 过滤机制：命中敏感词即由调用方决定
丢弃/替换。本项目未安装 ahocorasick，改用纯 Python 的首字符索引匹配：

- 加载时按「首字符 → 词列表」分组，同组内长词在前（长词先匹配，避免短词
  把长词截断误判）；
- has_hit(text) 逐字符取候选词，用 startswith 做精确子串匹配；
- 只检查长度不超过剩余文本的词，弹幕/短输入每次匹配微秒~毫秒级。

单字词条不参与匹配：keyboard.txt 里的 70 个单字词（獨/慾/屍 等）子串匹配
会严重误伤正常弹幕（"独特" / "欲望" / "尸体"），过滤从 2 字词开始。

用法：
    filt = KeyboardFilter()          # 默认读 data/keyboard.txt
    if filt.has_hit(text): ...       # True = 命中敏感词
"""

import os
from typing import Dict, List, Optional

from src.utils import config, console


class KeyboardFilter:
    def __init__(self, path: Optional[str] = None) -> None:
        self._by_first: Dict[str, List[str]] = {}
        self._count = 0
        self.load(path or self._default_path())

    @staticmethod
    def _default_path() -> str:
        return os.path.join(config.cfg.PROJECT_ROOT, "data", "keyboard.txt")

    def load(self, path: str) -> None:
        """（重新）加载词库；文件缺失/读取失败时置空（过滤不生效，不阻断主流程）。

        单字词条（len<=1）排除：误伤率远高于命中率（"獨"命中"独特"）。
        """
        self._by_first = {}
        self._count = 0
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    word = line.strip()
                    if len(word) <= 1:      # 跳过单字词条，避免误伤正常词
                        continue
                    self._by_first.setdefault(word[0], []).append(word)
                    self._count += 1
        except OSError as e:
            console.warn(f"[键盘过滤] 加载 {path} 失败：{e}（过滤未生效）")
            return
        for group in self._by_first.values():
            group.sort(key=len, reverse=True)

    @property
    def count(self) -> int:
        return self._count

    def has_hit(self, text: str) -> bool:
        """text 是否命中任一敏感词（精确子串匹配，大小写不敏感需自行 lower）。"""
        if not text or not self._by_first:
            return False
        text_len = len(text)
        for i in range(text_len):
            candidates = self._by_first.get(text[i])
            if not candidates:
                continue
            for word in candidates:
                # 用 startswith(text, i) 替代切片 startswith，避免每次创建子串
                if len(word) <= text_len - i and text.startswith(word, i):
                    return True
        return False
