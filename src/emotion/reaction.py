"""用户消息情绪分类器（严格移植 Soullink MessageReactionClassifier.ts）。

参考：e:\\AI\\vtuber\\情绪\\packages\\engine\\src\\reaction\\MessageReactionClassifier.ts
- 结构完全一致：按优先级排列的 if-else 正则匹配链 + 二次细分匹配 + neutral 兜底
- 每条规则返回完整 EmotionIntent（emotion / variant / intensity / context_tags / source_message）
- 情绪名对齐本项目 6 种基础情绪（开心/生气/疑惑/悲伤/害怕/厌恶）+ 中性兜底
  （emotion_state.VAD_PRESETS），原始细粒度情绪按合并映射归入基础情绪

用户输入到达后立即分类 → 让角色「先有反应、再开口说话」，
不用等 AI 生成回复。LLM 回复中的 [情绪:] 指令在此基础上继续叠加。
"""

import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple


@dataclass
class EmotionIntent:
    """一次情绪意图（对应 EmotionIntent.ts）。"""
    emotion: str                                   # 主情绪（中文名，查 VAD_PRESETS）
    intensity: float                               # 反应强度 0~1
    context_tags: List[str] = field(default_factory=list)  # 上下文标签（compliment/warm…）
    variant: Optional[str] = None                  # 同情绪内的表情变体
    natural_vad: Optional[Tuple[float, float, float]] = None  # 连续 VAD 坐标（Embedding 分类产出）
    source: Optional[str] = None                   # 分类来源（exact/embedding/neutral/fallback/None=规则）
    source_message: Optional[str] = None           # 源消息


class MessageReaction:
    """规则情绪分类器：消息 → EmotionIntent（正则实现）。"""

    def classify(self, message: str) -> EmotionIntent:
        """分类消息，返回情绪意图。"""
        text = message.strip()

        # 好消息 / 成功 → 开心
        if re.search(r"(过了|成功|赢了|拿下|通过|上岸|好消息|好耶|太棒)", text):
            return EmotionIntent(
                emotion="开心", variant="surprised_happy", intensity=0.85,
                context_tags=["user_good_news"], source_message=message)

        # 被夸奖 / 表达喜欢 → 开心
        if re.search(r"(喜欢你|可爱|好看|夸夸|贴贴|好温柔|真棒)", text):
            return EmotionIntent(
                emotion="开心", variant="bashful", intensity=0.8,
                context_tags=["compliment", "warm"], source_message=message)

        # 兴奋 / 高能量 → 开心
        if re.search(r"(兴奋|太爽|冲啊|炸了|激动|加油)", text):
            return EmotionIntent(
                emotion="开心", variant="sparkle", intensity=0.86,
                context_tags=["user_good_news"], source_message=message)

        # 累 / 难受 / 需要安慰（二次细分：疲惫→悲伤 vs 担忧→害怕）
        if re.search(r"(累|难受|不开心|崩溃|压力|困|疼)", text):
            if re.search(r"(累|困|没精神)", text):
                emotion, variant = "悲伤", "drained"
            else:
                emotion, variant = "害怕", "comfort"
            return EmotionIntent(
                emotion=emotion, variant=variant, intensity=0.75,
                context_tags=["user_tired", "warm"], source_message=message)

        # 伤心 → 悲伤
        if re.search(r"(难过|伤心|想哭|委屈|失落|呜呜)", text):
            return EmotionIntent(
                emotion="悲伤", variant="downcast", intensity=0.72,
                context_tags=["comfort"], source_message=message)

        # 焦虑 / 不安 → 害怕
        if re.search(r"(焦虑|慌|害怕|紧张|不安)", text):
            return EmotionIntent(
                emotion="害怕", variant="nervous", intensity=0.76,
                context_tags=["comfort"], source_message=message)

        # 疑问 / 好奇（二次细分：好奇 vs 困惑）→ 疑惑
        if re.search(r"(怎么|为什么|咋回事|啥|不懂|疑惑)", text):
            if re.search(r"(好奇|想知道|什么原因)", text):
                variant = "tilt"
            else:
                variant = "confused"
            return EmotionIntent(
                emotion="疑惑", variant=variant, intensity=0.68,
                context_tags=["question", "curious"], source_message=message)

        # 生气
        if re.search(r"(生气|气死|讨厌|烦|离谱)", text):
            return EmotionIntent(
                emotion="生气", variant="annoyed", intensity=0.62,
                context_tags=["annoyed"], source_message=message)

        # 兜底：中性
        return EmotionIntent(
            emotion="中性", variant="neutral_ack", intensity=0.35,
            context_tags=["normal_chat"], source_message=message)
