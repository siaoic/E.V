"""记忆后端抽象（MemoryBackend ABC）。

目标：让「写入/召回/更新/删除/衰减」与具体存储后端解耦。
- LiteMemoryBackend（src/llm/memory/lite.py）：纯标准库 SQLite + 本地哈希
  向量，零外部依赖，作为可选的轻量后端。
- MemUBackend（src/llm/memory/memu_compat.py）：把现有
  tools/memory/memory.MemoryManager 适配成同一接口，供未来按
  MEMORY_BACKEND 切换，不影响现有默认路径。

设计约束（贴合本项目规范）：
- 不引入任何第三方库（SQLite 用标准库 sqlite3，向量用 numpy 已是依赖）；
- 全部方法 async，与现有 asyncio 主循环一致；
- 返回 dict 统一含 id/content/similarity/namespace/user/confidence，
  便于上层格式化与判决链消费。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Optional


class MemoryBackend(ABC):
    """记忆后端统一接口。所有实现必须保证接口一致。"""

    @abstractmethod
    async def add(
        self,
        content: str,
        *,
        namespace: str,
        user: str,
        topic: str = "general",
        confidence: float = 0.8,
        metadata: Optional[dict] = None,
    ) -> int:
        """写入一条记忆，返回自增 ID。"""

    @abstractmethod
    async def recall(
        self,
        query: str,
        *,
        namespace: Optional[str] = None,
        top_k: int = 5,
        min_similarity: float = 0.3,
    ) -> list[dict]:
        """混合检索，返回 [{id, content, similarity, namespace, user, ...}]。"""

    @abstractmethod
    async def update(
        self,
        memory_id: int,
        *,
        content: Optional[str] = None,
        confidence: Optional[float] = None,
    ) -> bool:
        """更新记忆内容 / 置信度，返回是否命中。"""

    @abstractmethod
    async def delete(self, memory_id: int) -> bool:
        """删除一条记忆，返回是否命中。"""

    @abstractmethod
    async def list(self, namespace: Optional[str] = None, limit: int = 100) -> list[dict]:
        """列出记忆（可限定命名空间），按创建时间倒序。"""

    @abstractmethod
    async def count(self, namespace: Optional[str] = None) -> int:
        """记忆总数（可限定命名空间）。"""

    @abstractmethod
    async def decay(self) -> int:
        """对全部记忆应用时间衰减（topic 差异化），返回清理条数。"""

    @abstractmethod
    async def close(self) -> None:
        """释放资源（连接池等）。"""


# ---------- 便捷工具（后端无关） ----------

async def recall_as_text(
    backend: MemoryBackend,
    query: str,
    *,
    namespace: Optional[str] = None,
    top_k: int = 5,
    min_similarity: float = 0.3,
) -> str:
    """把召回结果格式化为纯文本（供 system prompt 注入）。

    兼容现有 memU 的 get_memory_prompt 消费形态：每条一行
    `- 内容（相似度 x.xx）`，无结果返回空串。
    """
    items = await backend.recall(
        query, namespace=namespace, top_k=top_k, min_similarity=min_similarity)
    if not items:
        return ""
    return "\n".join(
        f"- {m.get('content', '')}（相似度 {m.get('similarity', 0.0):.2f}）"
        for m in items
    )
