"""Embedding 情绪分类器（语料库版）：情绪只由 Embedding 语义判断。

与 Soullink classifier-embedding 思路一致，使用 6 种基础情绪 + 中性兜底的中文语料：
- 语料来自 defaultCorpus.ts（src/emotion/corpus.py，按映射合并为 7 组）
- 用户消息 → embedding 向量 → 与全部语料向量做余弦相似度
- 每情绪取最高相似度，胜出情绪超过阈值才生效，否则判为中性
- 不使用任何关键词规则 / 规则降级；
  Embedding 不可用（未初始化 / 查询失败）时直接不判情绪（中性）
- 语料向量结果缓存到 data/emotion_embedding_cache.npz，
  语料未变化时重启秒级完成，不重复调用 Embedding API

Embedding API 用 SiliconFlow（https://api-docs.siliconflow.cn/docs/api/embeddings-post）：
    POST {base_url}/embeddings
    headers: Authorization: Bearer <SILICONFLOW_API_KEY>
    json: { "input": "…"|[…], "model": "Qwen/Qwen3-Embedding-0.6B" }
"""

import asyncio
import os
import re
import sys
import threading
import time
import unicodedata
from dataclasses import dataclass
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from src.utils import console
from src.emotion.corpus import EMOTION_CORPUS
from src.emotion.state import get_vad_preset
from src.emotion.reaction import EmotionIntent

# 语料向量缓存文件（data/ 下）：语料未变时复用，避免每次启动重新嵌入。
# PyInstaller 打包后（sys.frozen）：__file__ 指向临时解压目录（_MEIPASS），
# 缓存必须落在 exe 所在目录，否则写入失败且退出即丢。
if getattr(sys, "frozen", False):
    _CACHE_PATH = os.path.join(
        os.path.dirname(sys.executable),
        "data", "emotion_embedding_cache.npz",
    )
else:
    _CACHE_PATH = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "data", "emotion_embedding_cache.npz",
    )

# ---- 每情绪的默认变体（对齐 defaultExamples.ts 首个 variant，供 naturalVAD 微调）----
_DEFAULT_VARIANT: Dict[str, str] = {
    "中性": "neutral_ack", "开心": "soft_smile", "疑惑": "confused",
    "悲伤": "downcast", "害怕": "nervous", "生气": "annoyed", "厌恶": "disgusted",
}

# 最高相似度低于该值 → 判中性（语料锚点与情绪语义接近，0.55 足够区分）
_DEFAULT_THRESHOLD = 0.55
_DEFAULT_BATCH = 32   # SiliconFlow embedding 单次批量（保守值，避免触发限流）

# 单条文本 embedding 的字符上限：部分本地服务上下文仅 512 token，
# 中文约 1.5 字符/token，300 字符 ≈ 450 token，留余量避免超长 400
_MAX_EMBED_CHARS = 300


def _all_anchors() -> List[tuple]:
    """全部锚点：语料库 → [(情绪, 文本), ...]。"""
    return [(emo, t) for emo, ts in EMOTION_CORPUS.items() for t in ts]


def _cache_key(anchors: List[tuple]) -> List[str]:
    return [normalize_emotion_text(t) for _, t in anchors]


def _elapsed_ms(t0: float) -> float:
    """自 t0（time.perf_counter）以来的毫秒数（耗时统计用）。"""
    return (time.perf_counter() - t0) * 1000


def _save_cache(keys: List[str], vectors: List[List[float]], dim: int) -> None:
    """缓存锚点向量 + 模型维度：维度随缓存持久化，重启免探针（省一次网络调用）。"""
    try:
        os.makedirs(os.path.dirname(_CACHE_PATH), exist_ok=True)
        np.savez(_CACHE_PATH, keys=np.array(keys),
                 vectors=np.asarray(vectors, dtype=np.float32),
                 dim=np.int32(dim))
    except Exception as e:
        console.warn(f"情绪语料向量缓存写入失败：{type(e).__name__}: {e}")


def _load_cache(keys: List[str]) -> Optional[Tuple[List[List[float]], int]]:
    """加载缓存，返回 (向量列表, 模型维度)；无缓存 / 键不匹配 / 缺维度 → None。"""
    try:
        if not os.path.exists(_CACHE_PATH):
            return None
        z = np.load(_CACHE_PATH, allow_pickle=False)
        if "dim" not in z:   # 旧格式缓存（无维度记录）→ 需重建
            return None
        cached_keys = list(z["keys"])
        cached_vecs = np.asarray(z["vectors"], dtype=np.float32)
        if (cached_keys != keys or cached_vecs.ndim != 2
                or len(cached_vecs) != len(keys)):
            return None
        return [vec.tolist() for vec in cached_vecs], int(z["dim"])
    except Exception:
        return None


# ---------- 工具 ----------

def _clamp01(v: float) -> float:
    return max(0.0, min(1.0, v))


def normalize_emotion_text(text: str) -> str:
    """对齐 normalizeEmotionText：NFKC + 小写 + 去除标点/符号/空白。"""
    norm = unicodedata.normalize("NFKC", text).strip().lower()
    compact = "".join(
        ch for ch in norm
        if unicodedata.category(ch)[0] not in ("P", "S") and not ch.isspace())
    return compact or re.sub(r"\s+", " ", norm)


def cosine_similarity(a: List[float], b: List[float]) -> float:
    """归一化点积（余弦相似度）。向量必须同维且已归一化。"""
    if len(a) != len(b):
        raise ValueError(f"Vector dimensions do not match: {len(a)} vs {len(b)}")
    return float(np.dot(a, b))


def normalize_embedding(vec: List[float]) -> Optional[List[float]]:
    """L2 归一化；无效向量（空 / 非有限 / 零范数）返回 None。"""
    arr = np.asarray(vec, dtype=np.float64)
    if arr.size == 0 or not np.all(np.isfinite(arr)):
        return None
    norm = float(np.linalg.norm(arr))
    if norm == 0:
        return None
    return (arr / norm).tolist()


# ---------- Embedding Provider（SiliconFlow）----------

# 模块级嵌入缓存（线程安全）：同一文本跨实例/跨调用复用，避免重复网络调用。
# embedding 是确定性函数，缓存无时效性问题；仅按容量上限防内存膨胀。
# 记忆检索查询嵌入由此从 ~200ms 网络调用降到 ~0ms，逼近 MemU 全本地 50-80ms。
_EMBED_CACHE_MAX = 4096
_EMBED_CACHE: "OrderedDict[str, List[float]]" = OrderedDict()
_EMBED_LOCK = threading.Lock()


def _lru_cache_get(cache: "OrderedDict", lock: threading.Lock, key) -> Any:
    """LRU 读取：命中时把 key 挪到末尾（最近使用），未命中返回 None。"""
    with lock:
        value = cache.get(key)
        if value is None:
            return None
        try:
            cache.move_to_end(key)
        except KeyError:
            pass
        return value


def _lru_cache_put(cache: "OrderedDict", lock: threading.Lock,
                   key, value, max_size: int) -> None:
    """LRU 写入：满时淘汰最旧的一半，避免"满了一下清空→全量冷启动抖动"。"""
    with lock:
        if key in cache:
            try:
                cache.move_to_end(key)
            except KeyError:
                pass
            cache[key] = value
            return
        if len(cache) >= max_size:
            drop_n = max(1, max_size // 2)
            try:
                for _ in range(drop_n):
                    cache.popitem(last=False)
            except KeyError:
                pass
        cache[key] = value


def _embed_cache_get(key: str) -> Optional[List[float]]:
    return _lru_cache_get(_EMBED_CACHE, _EMBED_LOCK, key)


def _embed_cache_put(key: str, vec: List[float]) -> None:
    _lru_cache_put(_EMBED_CACHE, _EMBED_LOCK, key, vec, _EMBED_CACHE_MAX)


# ---------- 单飞去重：并发相同文本只发一次网络请求 ----------
_EMBED_INFLIGHT: Dict[str, "asyncio.Future"] = {}


async def _embed_single_dedup(text: str, network_one) -> List[float]:
    """单条嵌入 + 并发去重（singleflight）。

    多个协程同时嵌入相同文本（如记忆检索与情绪分类并发处理同一句用户
    消息）时，只发起一次网络调用，其余协程复用结果——减少本地模型推理
    次数，缓解 CPU 竞争。发起方失败且无复用方时原样抛异常；有复用方时
    复用方各自重试一次（等价独立调用，不静默吞错）。
    """
    key = (text or "").strip()
    if not key:
        return await network_one(text)
    hit = _embed_cache_get(key)
    if hit is not None:
        return hit
    loop = asyncio.get_running_loop()
    fut = _EMBED_INFLIGHT.get(key)
    if fut is None:
        fut = loop.create_future()
        _EMBED_INFLIGHT[key] = fut
        try:
            vec = await network_one(text)
            if not fut.done():
                fut.set_result(vec)
            return vec
        except BaseException as e:
            if not fut.done():
                fut.set_exception(e)
            raise
        finally:
            _EMBED_INFLIGHT.pop(key, None)
    try:
        return await fut
    except Exception:
        # 发起方失败：本协程重试一次，等价各自独立请求
        return await network_one(text)


def _is_local_url(base_url: str) -> bool:
    """本地嵌入服务（如 llama.cpp）：base_url 指向本机环回地址。

    本地服务无需 API Key，configured 判定与 Authorization 头都要豁免。
    """
    host = (base_url or "").split("//")[-1].split("/")[0].lower()
    return host in ("127.0.0.1", "localhost", "::1", "[::1]", "0.0.0.0")


class SiliconFlowEmbeddingProvider:
    """OpenAI 兼容 Embedding provider（默认 SiliconFlow；可指向本地 llama.cpp）。

    async-first（对齐 memU 官方）：网络层用 httpx AsyncClient 原生 await，
    连接复用、可并发；同步写路径（add/update）另提供 embed_sync/batch_embed_sync
    用共享 httpx.Client（同一请求构造/解析逻辑，仅传输层不同）。

    API 参考：https://api-docs.siliconflow.cn/docs/api/embeddings-post
    llama.cpp：llama-server --embeddings 后 /v1/embeddings 同 OpenAI 格式，
    model 字段忽略（以服务端加载模型为准），无 API Key 也无需鉴权。
    Qwen3-VL-Embedding-8B 不支持 dimensions 参数；dimensions=None 时不传，
    用模型默认维度（换支持 dims 的模型可指定）。
    """

    def __init__(self, api_key: str, model: str = "Qwen/Qwen3-Embedding-0.6B",
                 base_url: str = "https://api.siliconflow.cn/v1",
                 dimensions: Optional[int] = None,
                 timeout: float = 30.0) -> None:
        self._api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.dimensions = dimensions
        self._timeout = timeout
        # 云端需 key；本地 llama.cpp 无需 key 也可用
        self._configured = bool(api_key and api_key.strip()) or _is_local_url(base_url)
        # 共享连接（惰性创建；AsyncClient 必须在事件循环内创建/使用）
        self._async_client = None  # httpx.AsyncClient
        self._sync_client = None   # httpx.Client

    @property
    def configured(self) -> bool:
        return self._configured

    async def aclose(self) -> None:
        """关闭 async 连接（进程退出时调用，避免警告）。"""
        if self._async_client is not None:
            try:
                await self._async_client.aclose()
            except Exception:
                pass
            self._async_client = None

    # ---- 请求构造 / 解析（async 与 sync 共用）----

    def _build_payload(self, input) -> dict:
        payload: dict = {"input": input, "model": self.model,
                         "encoding_format": "float"}
        if self.dimensions:
            payload["dimensions"] = self.dimensions
        return payload

    def _headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    def _check_response(self, resp) -> None:
        if resp.status_code == 501:
            raise RuntimeError(
                "Embedding 服务未开启嵌入模式：请用 llama-server --embeddings 启动"
                "（或检查 EMBEDDING_BASE_URL 是否指向支持 /v1/embeddings 的服务）")
        if resp.status_code != 200:
            text = (getattr(resp, "text", None)
                    or getattr(resp, "content", b"")[:300])
            raise RuntimeError(
                f"Embedding API failed with {resp.status_code}: {text}")

    def _parse_items(self, data) -> List[dict]:
        if not isinstance(data, list):
            raise RuntimeError(f"Embedding API returned unexpected payload: {data}")
        return sorted(data, key=lambda item: item.get("index", 0))

    # ---- 传输层 ----

    def _httpx_limits(self):
        import httpx  # 延迟导入
        return httpx.Limits(
            max_connections=20,
            max_keepalive_connections=10,
            keepalive_expiry=60.0,
        )

    async def _post_async(self, input) -> List[dict]:
        import httpx  # 延迟导入
        if self._async_client is None:
            self._async_client = httpx.AsyncClient(
                timeout=self._timeout,
                limits=self._httpx_limits(),
            )
        resp = await self._async_client.post(
            f"{self.base_url}/embeddings",
            headers=self._headers(), json=self._build_payload(input))
        self._check_response(resp)
        return self._parse_items(resp.json().get("data"))

    def _post_sync(self, input) -> List[dict]:
        import httpx  # 延迟导入
        if self._sync_client is None:
            self._sync_client = httpx.Client(
                timeout=self._timeout,
                limits=self._httpx_limits(),
            )
        resp = self._sync_client.post(
            f"{self.base_url}/embeddings",
            headers=self._headers(), json=self._build_payload(input))
        self._check_response(resp)
        return self._parse_items(resp.json().get("data"))

    # ---- 缓存（async / sync 共用同一缓存，确定性函数无时效问题）----

    def _plan_batch(self, keys: List[str]) -> Tuple[List[Optional[List[float]]], List[int]]:
        """按 keys 顺序填充缓存命中向量，未命中的下标收集到 missing（供批量请求）。"""
        out: List[Optional[List[float]]] = [None] * len(keys)
        missing: List[int] = []
        for i, k in enumerate(keys):
            hit = _embed_cache_get(k) if k else None
            if hit is not None:
                out[i] = hit
            else:
                missing.append(i)
        return out, missing

    def _fill_batch(self, out: List[Optional[List[float]]], missing: List[int],
                    keys: List[str], vectors: List[List[float]]) -> None:
        """把传输层返回的向量按 missing 顺序回填 out 并写入缓存。"""
        for i, vec in zip(missing, vectors):
            out[i] = vec
            if keys[i]:
                _embed_cache_put(keys[i], vec)

    async def embed(self, text: str) -> List[float]:
        key = (text or "").strip()
        if not key:
            return list((await self._post_async(key))[0]["embedding"])
        return await _embed_single_dedup(text, self._network_one)

    async def _network_one(self, text: str) -> List[float]:
        """单条网络嵌入（_embed_single_dedup 的发起方回调）：回填缓存 + 打日志。"""
        key = (text or "").strip()
        _t0 = time.perf_counter()
        vec = list((await self._post_async(key))[0]["embedding"])
        _embed_cache_put(key, vec)
        console.info(f"[Embed] 单条 {len(key)} 字 → 网络 {_elapsed_ms(_t0):.1f} ms")
        return vec

    async def batch_embed(self, texts: List[str]) -> List[List[float]]:
        if not texts:
            return []
        keys = [(t or "").strip() for t in texts]
        out, missing = self._plan_batch(keys)
        if missing:
            _t0 = time.perf_counter()
            items = await self._post_async([keys[i] for i in missing])
            self._fill_batch(out, missing, keys, [list(it["embedding"]) for it in items])
            console.info(
                f"[Embed] 批量 {len(missing)} 条 → 网络 {_elapsed_ms(_t0):.1f} ms")
        return [v for v in out if v is not None]

    def embed_sync(self, text: str) -> List[float]:
        key = (text or "").strip()
        if not key:
            return list(self._post_sync(key)[0]["embedding"])
        hit = _embed_cache_get(key)
        if hit is not None:
            return hit
        _t0 = time.perf_counter()
        vec = list(self._post_sync(key)[0]["embedding"])
        _embed_cache_put(key, vec)
        console.info(f"[Embed] 单条 {len(key)} 字 → 网络 {_elapsed_ms(_t0):.1f} ms（同步）")
        return vec

    def batch_embed_sync(self, texts: List[str]) -> List[List[float]]:
        if not texts:
            return []
        keys = [(t or "").strip() for t in texts]
        out, missing = self._plan_batch(keys)
        if missing:
            _t0 = time.perf_counter()
            items = self._post_sync([keys[i] for i in missing])
            self._fill_batch(out, missing, keys, [list(it["embedding"]) for it in items])
            console.info(
                f"[Embed] 批量 {len(missing)} 条 → 网络 {_elapsed_ms(_t0):.1f} ms（同步）")
        return [v for v in out if v is not None]


# ---------- 锚点 ----------

@dataclass
class _Anchor:
    text: str
    emotion: str                       # 中文情绪名（VAD_PRESETS 键）
    embedding: List[float]


class EmbeddingEmotionClassifier:
    """语义情绪分类器（语料库比对，无关键词规则 / 规则降级）。"""

    def __init__(self, provider: SiliconFlowEmbeddingProvider, *,
                 threshold: float = _DEFAULT_THRESHOLD,
                 batch_size: int = _DEFAULT_BATCH) -> None:
        self._provider = provider
        self._threshold = threshold
        self._batch_size = batch_size
        self._anchors: List[_Anchor] = []
        self._exact: Dict[str, _Anchor] = {}
        self._anchor_matrix: Optional[np.ndarray] = None  # N×D（每行一条归一化锚点）
        self._anchor_dim: Optional[int] = None            # 锚点模型输出维度
        self._initialized = False
        self._init_task: Optional[asyncio.Task] = None

    # ---------- 初始化 ----------

    async def _initialize(self) -> None:
        """异步初始化：全部语料锚点批量嵌入（首次调用 API，之后复用缓存）。

        缓存命中时维度已随缓存持久化，无需再发探针网络请求；仅缓存缺失
        （首启 / 语料变更 / 换模型）才需要探针探测当前模型维度。
        """
        # 超长锚点截断后再嵌入，防止触发服务上下文上限（400）
        texts = [(emo, t[:_MAX_EMBED_CHARS]) for emo, t in _all_anchors()]
        keys = _cache_key(texts)
        cached = _load_cache(keys)
        if cached is not None:
            vecs, dim = cached
        else:
            # 先探测当前 embedding 模型输出维度（单字，极低成本）
            probe = normalize_embedding(await self._provider.embed("维"))
            if probe is None:
                raise RuntimeError("Embedding provider returned an invalid probe vector")
            dim = len(probe)
            vecs = []
            for offset in range(0, len(texts), self._batch_size):
                batch = texts[offset:offset + self._batch_size]
                got = await self._provider.batch_embed([t for _, t in batch])
                if len(got) != len(batch):
                    raise RuntimeError(
                        f"Embedding provider returned {len(got)} vectors for a batch "
                        f"of {len(batch)} anchors")
                vecs.extend(got)
            _save_cache(keys, vecs, dim)
        self._anchor_dim = dim
        for (emo, text), vec in zip(texts, vecs):
            norm = normalize_embedding(vec)
            if norm is None:
                raise RuntimeError("Embedding provider returned an invalid vector")
            self._anchors.append(_Anchor(text=text, emotion=emo, embedding=norm))
        self._exact = {normalize_emotion_text(a.text): a for a in self._anchors}
        # 预构建锚点矩阵（与 _anchors 行序一致），classify 一次性点积替代逐条 Python 循环
        self._anchor_matrix = np.asarray(
            [a.embedding for a in self._anchors], dtype=np.float32)
        self._initialized = True

    async def initialize(self) -> bool:
        """异步初始化（网络调用原生 await，不占线程）。失败返回 False，本次不判情绪。"""
        if not self._provider.configured:
            return False
        if self._initialized:
            return True
        if self._init_task is None:
            self._init_task = asyncio.create_task(self._initialize())
        try:
            await self._init_task
            return self._initialized
        except Exception as e:
            console.warn(f"Embedding 初始化失败，本次不判情绪："
                         f"{type(e).__name__}: {e}")
            self._initialized = False
            return False
        finally:
            self._init_task = None

    # ---------- 分类 ----------

    async def classify(self, message: str) -> EmotionIntent:
        """语义情绪分类（原生 async：嵌入网络调用直接 await）。"""
        # 超长输入截断（与锚点同一上限），防止查询触发服务上下文 400
        text = message.strip()[:_MAX_EMBED_CHARS]
        if not self._initialized:
            return EmotionIntent(emotion="中性", intensity=0.35,
                                 context_tags=[], source="not-initialized",
                                 source_message=text)
        # 精确命中锚点 → 直接返回（无需查询）
        exact = self._exact.get(normalize_emotion_text(text))
        if exact:
            return self._intent(exact, text, 0.92, "exact")

        try:
            q = normalize_embedding(await self._provider.embed(text))
            if q is None:
                raise RuntimeError("Embedding provider returned an invalid query vector")
        except Exception as e:
            console.warn(f"Embedding 分类查询失败，本次不判情绪："
                         f"{type(e).__name__}: {e}")
            return EmotionIntent(emotion="中性", intensity=0.35,
                                 context_tags=[], source="embedding-failed",
                                 source_message=text)

        # 维度保护：缓存命中免探针后，若当前模型维度与缓存不一致（换模型），
        # 矩阵点积会维度不匹配崩溃——此处安全降级为中性并提示重建缓存
        if len(q) != self._anchor_dim:
            console.warn(
                f"Embedding 维度异常：模型输出 {len(q)} 维，锚点缓存 {self._anchor_dim} 维，"
                f"请删除 {_CACHE_PATH} 后重启自动重建")
            return EmotionIntent(emotion="中性", intensity=0.35,
                                 context_tags=[], source="embedding-failed",
                                 source_message=text)
        # 全部锚点一次矩阵点积取最高相似度（numpy argmax 返回首个最优，
        # 与原遍历"严格大于才更新"语义一致）；低于阈值判中性
        sims = self._anchor_matrix @ np.asarray(q, dtype=np.float32)
        best_idx = int(np.argmax(sims))
        best_anchor = self._anchors[best_idx]
        best_sim = float(sims[best_idx])
        if best_sim < self._threshold:
            return EmotionIntent(emotion="中性", intensity=0.35,
                                 context_tags=[], source="neutral",
                                 source_message=text)
        # 相似度 → 强度：阈值处 0.35，高分趋近 0.95
        intensity = _clamp01(0.35 + 0.6 * (best_sim - self._threshold) / 0.35)
        return self._intent(best_anchor, text, round(intensity, 2), "embedding")

    # ---------- 内部 ----------

    def _intent(self, anchor: _Anchor, text: str,
                intensity: float, source: str) -> EmotionIntent:
        variant = _DEFAULT_VARIANT.get(anchor.emotion)
        vad = get_vad_preset(anchor.emotion, variant)
        return EmotionIntent(emotion=anchor.emotion, intensity=intensity,
                             variant=variant, context_tags=[],
                             natural_vad=vad, source=source,
                             source_message=text)