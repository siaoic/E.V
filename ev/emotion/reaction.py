"""用户消息情绪分类器（完全向量实现，替代原正则规则链）。

- 不再使用任何正则表达式；分类完全由 EmbeddingEmotionClassifier 完成：
  把消息向量化后与六种基础情绪语料做余弦相似度比对，取相似度最高的情绪。
- 每条结果返回完整 EmotionIntent（emotion / intensity / context_tags /
  variant / natural_vad / source / source_message）
- 未配置可用嵌入服务时返回中性兜底（不抛异常），且不退化到任何规则/正则。

用户输入到达后立即分类 → 让角色「先有反应、再开口说话」，
不用等 AI 生成回复。LLM 回复中的 [情绪:] 指令在此基础上继续叠加。
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from ev.emotion.state import VAD_PRESETS
from ev.llm.utils.embedding import (
    EmbeddingEmotionClassifier,
    SiliconFlowEmbeddingProvider,
)


# 向量分类只输出「主情绪名 + 相似度得分」；这里补齐角色表现所需的辅助字段。
# 若后续需要更细粒度情绪，可扩展为按语料簇/阈值再细分 variant。
_EMOTION_META: Dict[str, Dict[str, object]] = {
    "开心": {
        "variant": "sparkle",
        "context_tags": ["user_good_news"],
        "intensity": 0.82,
    },
    "生气": {
        "variant": "annoyed",
        "context_tags": ["annoyed"],
        "intensity": 0.72,
    },
    "疑惑": {
        "variant": "confused",
        "context_tags": ["question", "curious"],
        "intensity": 0.68,
    },
    "悲伤": {
        "variant": "downcast",
        "context_tags": ["comfort"],
        "intensity": 0.74,
    },
    "害怕": {
        "variant": "nervous",
        "context_tags": ["comfort"],
        "intensity": 0.76,
    },
    "厌恶": {
        "variant": "disgust",
        "context_tags": ["disgust"],
        "intensity": 0.70,
    },
}


@dataclass
class EmotionIntent:
    """一次情绪意图（对应 EmotionIntent.ts）。"""
    emotion: str                                   # 主情绪（中文名，查 VAD_PRESETS）
    intensity: float = 0.35                        # 反应强度 0~1
    context_tags: List[str] = field(default_factory=list)  # 上下文标签（compliment/warm…）
    variant: Optional[str] = None                  # 同情绪内的表情变体
    natural_vad: Optional[Tuple[float, float, float]] = None  # 连续 VAD 坐标（Embedding 分类产出）
    source: Optional[str] = None                   # 分类来源（embedding/neutral/fallback）
    source_message: Optional[str] = None           # 源消息


class MessageReaction:
    """向量情绪分类器：消息 → EmotionIntent（Embedding 实现，无正则）。"""

    def __init__(
        self,
        classifier: Optional[EmbeddingEmotionClassifier] = None,
    ) -> None:
        # 允许外部注入已初始化的分类器（避免重复 embed 语料）；
        # 未注入时在第一次 classify 时懒构建默认 provider + classifier。
        self._classifier: Optional[EmbeddingEmotionClassifier] = classifier
        self._init_attempted = classifier is not None

    def set_classifier(
        self,
        classifier: Optional[EmbeddingEmotionClassifier],
    ) -> None:
        """注入外部已初始化好的 embedding 分类器（共享模型，避免重复初始化）。"""
        self._classifier = classifier
        self._init_attempted = classifier is not None

    def _ensure_classifier(self) -> Optional[EmbeddingEmotionClassifier]:
        """懒初始化默认分类器；失败返回 None（只尝试一次）。"""
        if self._classifier is not None:
            return self._classifier
        if self._init_attempted:
            return None
        self._init_attempted = True

        provider = SiliconFlowEmbeddingProvider()
        if not provider.configured:
            return None

        classifier = EmbeddingEmotionClassifier(provider)
        if not classifier.initialize_sync():
            return None

        self._classifier = classifier
        return classifier

    def classify(self, message: str) -> EmotionIntent:
        """分类消息，返回情绪意图（纯向量，无正则）。"""
        text = (message or "").strip()
        if not text:
            return self._neutral(text, source="neutral")

        classifier = self._ensure_classifier()
        if classifier is None:
            return self._neutral(text, source="fallback")

        try:
            result = classifier.classify_sync(text)
        except Exception:
            return self._neutral(text, source="fallback")

        emotion = getattr(result, "emotion", "中性") or "中性"
        score = float(getattr(result, "score", 0.0) or 0.0)
        return self._build(emotion, score, text)

    def _build(
        self,
        emotion: str,
        score: float,
        message: str,
    ) -> EmotionIntent:
        """把向量分类结果补全为完整 EmotionIntent。"""
        meta = _EMOTION_META.get(emotion, {})
        base_intensity = float(meta.get("intensity", 0.35))
        # 余弦得分越高，强度越强；并限制在合理区间。
        intensity = max(0.35, min(1.0, base_intensity + score * 0.15))
        return EmotionIntent(
            emotion=emotion,
            intensity=round(intensity, 2),
            context_tags=list(meta.get("context_tags", [])),
            variant=meta.get("variant"),
            natural_vad=VAD_PRESETS.get(emotion),
            source="embedding",
            source_message=message,
        )

    @staticmethod
    def _neutral(message: str, source: str) -> EmotionIntent:
        """向量分类不可用时返回的中性兜底。"""
        return EmotionIntent(
            emotion="中性",
            intensity=0.35,
            context_tags=["normal_chat"],
            variant="neutral_ack",
            natural_vad=None,
            source=source,
            source_message=message,
        )
