"""MemUBackend：把现有 memU 记忆管理器适配为 MemoryBackend ABC。

目的：对外提供与 LiteMemoryBackend 一致的接口，供未来按
MEMORY_BACKEND=memu/lite 无感切换。当前默认路径（模块级函数直接调用）
不受影响；本类仅在显式构造时使用。

适配映射：
- add     → MemoryManager.commit_recall_files（name 幂等去重由 memU 负责）
- recall  → MemoryManager._recall_similar_structured（复用段索引，返回
            [{id, content, similarity}]，与判决链同源）
- delete  → delete_memories_async
- list    → list_files
- count   → count
- decay   → 模块级 decay_stale_memories
"""

from __future__ import annotations

from typing import Any, Optional

from src.llm.memory.base import MemoryBackend
from src.llm.memory.namespace import NS_SHARED_PROFILE


class MemUBackend(MemoryBackend):
    """现有 memU MemoryManager 的 ABC 适配层。"""

    def __init__(self, manager: Any = None) -> None:
        # 懒获取：构造时不强制初始化 memU（避免缺依赖环境启动即崩）
        self._manager = manager

    def _mgr(self):
        if self._manager is None:
            from tools.memory.memory import get_manager

            self._manager = get_manager()
        return self._manager

    async def add(
        self,
        content: str,
        *,
        namespace: str = NS_SHARED_PROFILE,
        user: str = "anonymous",
        topic: str = "general",
        confidence: float = 0.8,
        metadata: Optional[dict] = None,
    ) -> int:
        mgr = self._mgr()
        meta = {"namespace": namespace, "topic": topic, **((metadata or {}))}
        result = await mgr.commit_recall_files([{
            "name": topic,
            "content": content,
            "user": user,
            "metadata": meta,
        }])
        files = result.get("recall_files") or []
        if not files:
            return -1  # memU 判定重复，未入库
        try:
            return int(files[0].get("id"))
        except (TypeError, ValueError):
            return -1

    async def recall(
        self,
        query: str,
        *,
        namespace: Optional[str] = None,
        top_k: int = 5,
        min_similarity: float = 0.3,
    ) -> list[dict]:
        mgr = self._mgr()
        try:
            items = await mgr._recall_similar_structured(query, "", top_k)
        except Exception:
            return []
        results = []
        for m in items:
            if m.get("similarity", 0.0) < min_similarity:
                continue
            if namespace and m.get("namespace") not in (None, namespace):
                continue
            results.append(m)
        return results[:top_k]

    async def update(
        self,
        memory_id: int,
        *,
        content: Optional[str] = None,
        confidence: Optional[float] = None,
    ) -> bool:
        # memU 无独立 update 语义：内容更新走「删旧补新」
        if content is not None:
            deleted = await self.delete(memory_id)
            if not deleted:
                return False
            await self.add(content, namespace=NS_SHARED_PROFILE, user="anonymous")
            return True
        return False

    async def delete(self, memory_id: int) -> bool:
        mgr = self._mgr()
        return await mgr.delete_memories_async([str(memory_id)]) > 0

    async def list(self, namespace: Optional[str] = None, limit: int = 100) -> list[dict]:
        mgr = self._mgr()
        files = mgr.list_files(limit=limit)
        if namespace:
            files = [f for f in files if f.get("metadata", {}).get("namespace") == namespace]
        return files

    async def count(self, namespace: Optional[str] = None) -> int:
        mgr = self._mgr()
        if namespace is None:
            return mgr.count()
        files = await self.list(namespace=namespace, limit=100000)
        return len(files)

    async def decay(self) -> int:
        from tools.memory.memory import decay_stale_memories

        return decay_stale_memories()

    async def close(self) -> None:
        try:
            await self._mgr().aclose()
        except Exception:
            pass
