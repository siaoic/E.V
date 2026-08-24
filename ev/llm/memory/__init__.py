"""记忆系统：后端抽象 + Mem0 判决链 + 命名空间/衰减/防护 + L4 Provider 编排。

分层：
- base.py         MemoryBackend ABC + 文本格式化工具
- lite.py         LiteMemoryBackend（SQLite + 哈希向量，零第三方依赖）
- memu_compat.py  MemUBackend（现有 memU 的 ABC 适配层）
- lifecycle.py    Mem0 判决链（ADD/UPDATE/DELETE/IGNORE，规则预筛先行）
- namespace.py    命名空间与主题推断（纯规则）
- decay.py        按 topic 差异化时间衰减（后端无关）
- lore_guard.py   Lore 泄漏防护 + 记忆注入铁律（防 OOC）
- curated.py      L2 纯文本长期记忆（MEMORY.md/USER.md + 冻结快照）
- session.py      L3 会话历史（SQLite + FTS5 全文检索）
- provider.py     L4 MemoryProvider ABC（可插拔语义后端）
- manager.py      L4 MemoryManager 编排（注册/召回/写入/边界事件）
- memu_provider.py memU 内置 Provider 适配（观察现有直连调用链）

接入策略（硬约束：默认行为 100% 不变）：
- 现有路径（tools.memory.memory 模块级函数）不受影响；
- MEMORY_BACKEND=memu（默认）继续走模块级函数；
- MEMORY_BACKEND=lite 时才构造 LiteMemoryBackend；
- MEMORY_LIFECYCLE_ENABLED=true 时判决链接入现有 commit_recall_files；
- L2/L3/L4 全部默认开启且为纯增量（不改变任何现有链路行为）。
"""

from __future__ import annotations

from typing import Optional

from ev.llm.memory.base import MemoryBackend, recall_as_text
from ev.llm.memory.lifecycle import LifecycleEngine
from ev.llm.memory.manager import MemoryManager, get_memory_manager
from ev.llm.memory.provider import MemoryProvider

__all__ = [
    "MemoryBackend", "LifecycleEngine", "recall_as_text",
    "MemoryProvider", "MemoryManager", "get_memory_manager",
    "create_memory_backend",
]


def create_memory_backend(
    backend: Optional[str] = None,
    *,
    db_path: str = "data/memories/lite.db",
    **kwargs,
) -> MemoryBackend:
    """按 MEMORY_BACKEND 创建后端实例（lite / memu），默认 memu。

    memu：包装现有 MemoryManager（保持默认行为）；
    lite：SQLite + 本地哈希向量（零外部 Embedding 依赖）。
    未知取值回退 memu（与现有行为一致）。
    """
    name = (backend or "").strip().lower()
    if name == "lite":
        from ev.llm.memory.lite import LiteMemoryBackend

        return LiteMemoryBackend(db_path, **kwargs)
    from ev.llm.memory.memu_compat import MemUBackend

    return MemUBackend()
