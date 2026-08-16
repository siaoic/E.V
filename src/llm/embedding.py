"""情绪分类 embedding：基于 memU 网关（memu.embedding.gateway）的全新封装。

- SiliconFlowEmbeddingProvider：包装 memU embedding client，提供同步/异步
  文本向量化（带 LRU 缓存），兼容任意 OpenAI 协议端点（SiliconFlow /
  本地 llama.cpp 等）。
- EmbeddingEmotionClassifier：把文本向量化后与六种基础情绪语料向量做
  余弦相似度比对，返回最近的情绪意图（intent.emotion）。

对外接口（emotion_actor 依赖）：
    provider = SiliconFlowEmbeddingProvider(api_key, model, base_url)
    provider.configured → bool
    classifier = EmbeddingEmotionClassifier(provider)
    await classifier.initialize() → bool
    intent = await classifier.classify(text)  # intent.emotion
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
from collections import OrderedDict
from typing import Any

from src.utils import config, console
from tools.memory.memory import _ensure_memu_path

# memU 引擎路径注入（仅动 sys.path，memU 本体由 _build_client 惰性导入，
# 避免缺 httpx/openai 的环境在模块导入期就崩溃）
_ensure_memu_path()

# 六种基础情绪（与 emotion_actor.EMOTIONS 对齐）
EMOTIONS = ["开心", "生气", "疑惑", "悲伤", "害怕", "厌恶"]

# 每种情绪的示例语料（用于初始化时生成情绪向量）
_EMOTION_CORPUS: dict[str, list[str]] = {
    "开心": ["太棒了", "好开心", "笑死我了", "真让人高兴", "太喜欢了", "今天运气真好"],
    "生气": ["气死我了", "太过分了", "烦死了", "你怎么这样", "让人恼火", "真让人火大"],
    "疑惑": ["什么意思", "不太明白", "这是为什么", "搞不懂", "真的假的", "怎么回事"],
    "悲伤": ["好难过", "有点失落", "伤心", "心里难受", "唉", "想哭"],
    "害怕": ["好害怕", "吓死我了", "好恐怖", "有点慌", "怕死了", "太吓人了"],
    "厌恶": ["好恶心", "真讨厌", "烦人", "受不了", "厌恶", "敬而远之"],
}

_CACHE_CAPACITY = 4096


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """余弦相似度（numpy 向量化）。"""
    import numpy as np

    vector_a = np.asarray(a, dtype=float)
    vector_b = np.asarray(b, dtype=float)
    denom = float(np.linalg.norm(vector_a) * np.linalg.norm(vector_b))
    if denom == 0:
        return 0.0
    return float(np.dot(vector_a, vector_b) / denom)


def _run_sync(coro):
    """在同步上下文执行协程（无运行 loop 直接跑；有则放入线程池）。"""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(lambda: asyncio.run(coro)).result()


class SiliconFlowEmbeddingProvider:
    """基于 memU embedding client 的向量化服务（OpenAI 兼容端点）。"""

    def __init__(self, api_key: str = "", model: str = "", base_url: str = "") -> None:
        self.api_key = (api_key or config.cfg.EMBEDDING_API_KEY or "").strip()
        self.model = (model or config.cfg.EMBEDDING_MODEL or "").strip()
        self.base_url = (base_url or config.cfg.EMBEDDING_BASE_URL or "").strip()
        self._client: Any = None
        self._client_lock = threading.Lock()
        self._cache: OrderedDict[str, list[float]] = OrderedDict()

    @property
    def configured(self) -> bool:
        """配置可用（有 API Key，或本地端点免 Key）。"""
        return self._build_client() is not None

    def _build_client(self) -> Any:
        """懒构建 memU embedding client（线程安全）；失败返回 None。"""
        if self._client is None:
            with self._client_lock:
                if self._client is None:
                    if not (self.api_key or self._is_local(self.base_url)):
                        return None
                    try:
                        from memu.app.settings import EmbeddingConfig
                        from memu.embedding.gateway import build_embedding_client

                        cfg = EmbeddingConfig(
                            provider="openai",
                            client_backend="sdk",
                            base_url=self.base_url,
                            api_key=self.api_key or "local",
                            embed_model=self.model,
                            embed_batch_size=8,
                            embed_dimensions=config.cfg.EMBEDDING_DIMENSIONS,
                        )
                        self._client = build_embedding_client(cfg)
                    except Exception as e:
                        console.dim(f"embedding 客户端初始化失败：{e}")
                        return None
        return self._client

    @staticmethod
    def _is_local(base_url: str) -> bool:
        return "127.0.0.1" in base_url or "localhost" in base_url or "0.0.0.0" in base_url

    def _cache_set(self, text: str, vector: list[float]) -> None:
        self._cache[text] = vector
        if len(self._cache) > _CACHE_CAPACITY:
            for _ in range(_CACHE_CAPACITY // 2):
                self._cache.popitem(last=False)

    # ---------- 异步接口 ----------

    async def embed(self, text: str) -> list[float]:
        """单条文本 → 向量（命中缓存直接返回）。"""
        text = (text or "").strip()
        if not text:
            return []
        cached = self._cache.get(text)
        if cached is not None:
            return cached
        client = self._build_client()
        if client is None:
            raise RuntimeError("embedding client 未配置")
        vectors, _ = await client.embed([text])
        vector = list(vectors[0])
        self._cache_set(text, vector)
        return vector

    async def batch_embed(self, texts: list[str]) -> list[list[float]]:
        """批量文本 → 向量（先查缓存，剩余部分批量请求）。"""
        texts = [(t or "").strip() for t in texts]
        result: list[list[float]] = []
        pending: list[tuple[int, str]] = []
        for index, text in enumerate(texts):
            if not text:
                result.append([])
                continue
            cached = self._cache.get(text)
            if cached is not None:
                result.append(cached)
            else:
                pending.append((index, text))
                result.append([])  # 占位，稍后填充
        if pending:
            client = self._build_client()
            if client is None:
                raise RuntimeError("embedding client 未配置")
            vectors, _ = await client.embed([text for _, text in pending])
            for (index, text), vector in zip(pending, vectors, strict=True):
                vector = list(vector)
                result[index] = vector
                self._cache_set(text, vector)
        return result

    # ---------- 同步桥接（线程池内跑异步） ----------

    def embed_sync(self, text: str) -> list[float]:
        return _run_sync(self.embed(text))

    def batch_embed_sync(self, texts: list[str]) -> list[list[float]]:
        return _run_sync(self.batch_embed(texts))

    async def aclose(self) -> None:
        """关闭底层 client（若其提供 close/aclose）。"""
        client = self._client
        if client is None:
            return
        closer = getattr(client, "aclose", None) or getattr(client, "close", None)
        if closer is None:
            return
        try:
            result = closer()
            if asyncio.iscoroutine(result):
                await result
        except Exception:
            pass


class EmotionIntent:
    """情绪分类结果。"""

    def __init__(self, emotion: str, score: float) -> None:
        self.emotion = emotion
        self.score = score


class EmbeddingEmotionClassifier:
    """把文本向量化后与情绪语料向量比对，输出最近情绪。"""

    def __init__(self, provider: SiliconFlowEmbeddingProvider) -> None:
        self._provider = provider
        self._emotion_vectors: dict[str, list[float]] = {}
        self._ready = False

    async def initialize(self) -> bool:
        """对情绪语料批量向量化，构建比对基准。"""
        if not self._provider.configured:
            return False
        try:
            corpus_texts = [
                f"{emotion}：{'、'.join(examples)}"
                for emotion, examples in _EMOTION_CORPUS.items()
            ]
            vectors = await self._provider.batch_embed(corpus_texts)
            self._emotion_vectors = {
                emotion: vector
                for emotion, vector in zip(EMOTIONS, vectors, strict=True)
                if vector
            }
            self._ready = bool(self._emotion_vectors)
        except Exception as e:
            console.dim(f"情绪分类器初始化失败：{e}")
            self._ready = False
        return self._ready

    async def classify(self, text: str) -> EmotionIntent:
        """返回最近情绪的 EmotionIntent（未初始化抛异常）。"""
        if not self._ready:
            raise RuntimeError("情绪分类器尚未初始化")
        vector = await self._provider.embed(text)
        best_emotion = "中性"
        best_score = 0.0
        for emotion, ref in self._emotion_vectors.items():
            score = _cosine_similarity(vector, ref)
            if score > best_score:
                best_emotion, best_score = emotion, score
        return EmotionIntent(best_emotion, best_score)


__all__ = [
    "SiliconFlowEmbeddingProvider",
    "EmbeddingEmotionClassifier",
    "EmotionIntent",
    "EMOTIONS",
]
