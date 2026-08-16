"""LiteMemoryBackend：轻量记忆后端（纯标准库 + numpy，零第三方依赖）。

定位：
- 与现有 memU 后端并存的可选实现，通过 MEMORY_BACKEND=lite 切换；
- SQLite 存储 + 本地哈希向量（HashEncoder），不依赖外部 Embedding 服务，
  离线可用、启动快；
- 命名空间 / 主题 / 时间衰减原生支持（namespace.py / decay.py）。

哈希向量引擎（HashEncoder）说明：
- 1-gram（中文字符/英文词）+ 2-gram → 稳定哈希（md5）投影到固定维度，
  每个 token 激活 k 维，L2 归一化；
- 与 Firefly LocalEmbeddingEngine 思路一致：零模型、零下载、确定性
  （同文本同向量，跨进程稳定）。
- 语义精度弱于真 Embedding，但配合 2-gram 关键词 boost 与近期旁路，
  足够支撑直播轻量记忆召回。

线程安全：sqlite3 连接固定在线程池（check_same_thread=False）+ 互斥锁；
所有 IO 经 asyncio.to_thread 移出事件循环，避免阻塞（项目约定）。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import sqlite3
import threading
import time
from typing import Any, Optional

import numpy as np

from src.llm.memory.base import MemoryBackend
from src.llm.memory.decay import competitive_decay, should_compete
from src.llm.memory.namespace import NS_SHARED_PROFILE, sanitize_namespace

_SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    namespace TEXT NOT NULL,
    user TEXT NOT NULL,
    content TEXT NOT NULL,
    topic TEXT NOT NULL DEFAULT 'general',
    confidence REAL NOT NULL DEFAULT 0.8,
    created_at REAL NOT NULL,
    last_accessed REAL NOT NULL DEFAULT 0,
    access_count INTEGER NOT NULL DEFAULT 0,
    source TEXT,
    metadata TEXT,
    hash_vec BLOB
);
CREATE INDEX IF NOT EXISTS idx_ns_user ON memories(namespace, user);
CREATE INDEX IF NOT EXISTS idx_created ON memories(created_at);
"""

# 召回候选上限（防全表扫描撑爆内存；超出按创建时间取新）
_RECALL_CANDIDATE_LIMIT = 5000
# 近期记忆旁路窗口（秒）：7 天内访问过的记忆强制加分
_RECENT_WINDOW_SEC = 7 * 86400
_RECENT_BOOST = 1.5
# 关键词 boost：query 2-gram 命中 content 的增量（上限）
_KEYWORD_BOOST = 0.08
_KEYWORD_BOOST_CAP = 0.2


def _stable_hash(text: str) -> int:
    """跨进程稳定的字符串哈希（md5 前 8 字节，不受 PYTHONHASHSEED 影响）。"""
    return int.from_bytes(hashlib.md5(text.encode("utf-8")).digest()[:8], "big")


def _tokenize(text: str) -> tuple[list[str], list[str]]:
    """切分为 1-gram（中文字符 / 英数字词）与相邻 2-gram。"""
    unigrams: list[str] = []
    for m in re.finditer(r"[A-Za-z0-9]+", text):
        unigrams.append(m.group(0))
    for ch in text:
        if "\u4e00" <= ch <= "\u9fff":
            unigrams.append(ch)
    bigrams = [a + b for a, b in zip(unigrams[:-1], unigrams[1:])] if len(unigrams) > 1 else []
    return unigrams, bigrams


class HashEncoder:
    """哈希投影向量编码器：确定性、零模型、零下载。"""

    def __init__(self, dim: int = 384, k: int = 4) -> None:
        self.dim = dim
        self.k = k

    def encode(self, text: str) -> np.ndarray:
        unigrams, bigrams = _tokenize(text or "")
        vec = np.zeros(self.dim, dtype=np.float32)
        for token in unigrams + bigrams:
            for i in range(self.k):
                idx = _stable_hash(f"{token}_{i}") % self.dim
                vec[idx] += 1.0
        norm = float(np.linalg.norm(vec))
        if norm > 0:
            vec /= norm
        return vec

    def blob(self, text: str) -> bytes:
        return self.encode(text).tobytes()


class LiteMemoryBackend(MemoryBackend):
    """SQLite + 哈希向量的轻量记忆后端。"""

    def __init__(
        self,
        db_path: str,
        *,
        dim: int = 384,
        min_similarity: float = 0.3,
    ) -> None:
        self._db_path = str(db_path)
        self._encoder = HashEncoder(dim=dim)
        self._min_similarity = min_similarity
        self._lock = threading.Lock()
        self._closed = False
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    # ---------- 私有：同步执行（线程池外） ----------

    def _sync_add(
        self,
        content: str,
        *,
        namespace: str,
        user: str,
        topic: str,
        confidence: float,
        metadata: Optional[dict],
        source: str,
    ) -> int:
        now = time.time()
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO memories (namespace, user, content, topic, confidence, "
                "created_at, last_accessed, access_count, source, metadata, hash_vec) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)",
                (namespace, user, content, topic, confidence, now, now, source,
                 json.dumps(metadata or {}, ensure_ascii=False),
                 self._encoder.blob(content)),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def _sync_recall(
        self,
        query: str,
        *,
        namespace: Optional[str],
        top_k: int,
        min_similarity: float,
    ) -> list[dict]:
        sql = "SELECT id, namespace, user, content, topic, confidence, created_at, " \
              "last_accessed, access_count, metadata, hash_vec FROM memories"
        params: list[Any] = []
        if namespace:
            sql += " WHERE namespace = ?"
            params.append(namespace)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(_RECALL_CANDIDATE_LIMIT)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()

        qvec = self._encoder.encode(query)
        q_tokens, q_bigrams = _tokenize(query)
        q_bigram_set = set(q_bigrams)
        now = time.time()
        results: list[dict] = []
        for row in rows:
            (mem_id, ns, user, content, topic, confidence, created_at,
             last_accessed, access_count, metadata, vec_blob) = row
            if not vec_blob:
                continue
            cvec = np.frombuffer(vec_blob, dtype=np.float32)
            sim = float(np.dot(qvec, cvec))  # 已 L2 归一化，点积即余弦
            if sim < min_similarity:
                continue
            # 关键词 boost：query 2-gram 命中 content 加微分（上限封顶）
            if q_bigram_set:
                hit = sum(1 for bg in q_bigram_set if bg in content)
                if hit:
                    sim += min(_KEYWORD_BOOST * hit, _KEYWORD_BOOST_CAP)
            # 近期访问旁路：7 天内访问过的记忆加分
            if last_accessed and now - last_accessed < _RECENT_WINDOW_SEC:
                sim *= _RECENT_BOOST
            results.append({
                "id": mem_id,
                "namespace": ns,
                "user": user,
                "content": content,
                "topic": topic,
                "confidence": confidence,
                "created_at": created_at,
                "last_accessed": last_accessed,
                "access_count": access_count,
                "metadata": json.loads(metadata) if metadata else {},
                "similarity": round(sim, 4),
            })
        results.sort(key=lambda m: -m["similarity"])
        return results[:top_k]

    def _sync_update(self, memory_id: int, content: Optional[str], confidence: Optional[float]) -> bool:
        if content is None and confidence is None:
            return False
        sets: list[str] = []
        params: list[Any] = []
        if content is not None:
            sets.append("content = ?")
            params.append(content)
            sets.append("hash_vec = ?")
            params.append(self._encoder.blob(content))
        if confidence is not None:
            sets.append("confidence = ?")
            params.append(float(confidence))
        params.append(memory_id)
        with self._lock:
            cur = self._conn.execute(f"UPDATE memories SET {', '.join(sets)} WHERE id = ?", params)
            self._conn.commit()
            return cur.rowcount > 0

    def _sync_touch(self, memory_id: int) -> None:
        """召回命中时刷新访问时间与次数（Firefly touch_memory）。"""
        with self._lock:
            self._conn.execute(
                "UPDATE memories SET last_accessed = ?, access_count = access_count + 1 "
                "WHERE id = ?",
                (time.time(), memory_id),
            )
            self._conn.commit()

    def _sync_delete(self, memory_id: int) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
            self._conn.commit()
            return cur.rowcount > 0

    def _sync_list(self, namespace: Optional[str], limit: int) -> list[dict]:
        sql = "SELECT id, namespace, user, content, topic, confidence, created_at, " \
              "last_accessed, access_count, metadata FROM memories"
        params: list[Any] = []
        if namespace:
            sql += " WHERE namespace = ?"
            params.append(namespace)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [{
            "id": r[0], "namespace": r[1], "user": r[2], "content": r[3],
            "topic": r[4], "confidence": r[5], "created_at": r[6],
            "last_accessed": r[7], "access_count": r[8],
            "metadata": json.loads(r[9]) if r[9] else {},
        } for r in rows]

    def _sync_count(self, namespace: Optional[str]) -> int:
        sql = "SELECT COUNT(*) FROM memories"
        params: list[Any] = []
        if namespace:
            sql += " WHERE namespace = ?"
            params.append(namespace)
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
        return int(row[0]) if row else 0

    # ---------- MemoryBackend 实现 ----------

    async def add(
        self,
        content: str,
        *,
        namespace: str = NS_SHARED_PROFILE,
        user: str = "anonymous",
        topic: str = "general",
        confidence: float = 0.8,
        metadata: Optional[dict] = None,
        source: str = "",
    ) -> int:
        mem_id = await asyncio.to_thread(
            self._sync_add, content,
            namespace=sanitize_namespace(namespace), user=user,
            topic=topic, confidence=confidence, metadata=metadata, source=source,
        )
        # 竞争衰减：可替换事实类主题，新记忆入库后压制同 topic 旧记忆
        if should_compete(topic):
            await competitive_decay(self, topic=topic, new_id=mem_id,
                                    namespace=sanitize_namespace(namespace))
        return mem_id

    async def recall(
        self,
        query: str,
        *,
        namespace: Optional[str] = None,
        top_k: int = 5,
        min_similarity: Optional[float] = None,
    ) -> list[dict]:
        threshold = self._min_similarity if min_similarity is None else min_similarity
        results = await asyncio.to_thread(
            self._sync_recall, query, namespace=namespace,
            top_k=top_k, min_similarity=threshold,
        )
        # 召回命中即 touch（刷新访问时间，供近期旁路使用）
        for mem in results:
            await asyncio.to_thread(self._sync_touch, int(mem["id"]))
        return results

    async def update(
        self,
        memory_id: int,
        *,
        content: Optional[str] = None,
        confidence: Optional[float] = None,
    ) -> bool:
        return await asyncio.to_thread(self._sync_update, memory_id, content, confidence)

    async def delete(self, memory_id: int) -> bool:
        return await asyncio.to_thread(self._sync_delete, int(memory_id))

    async def list(self, namespace: Optional[str] = None, limit: int = 100) -> list[dict]:
        return await asyncio.to_thread(self._sync_list, namespace, limit)

    async def count(self, namespace: Optional[str] = None) -> int:
        return await asyncio.to_thread(self._sync_count, namespace)

    async def decay(self) -> int:
        from src.llm.memory.decay import decay_stale_memories

        return await decay_stale_memories(self)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        with self._lock:
            self._conn.close()
