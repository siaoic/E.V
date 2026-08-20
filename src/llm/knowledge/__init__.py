"""知识库模块（防幻觉）：知识金字塔 + 信号闸门 + 混合检索。

对标 Firefly 的知识层与 E.V_REFACTOR.md §3.1：
- data/knowledge/curated_cards  →  L0a 精选卡片（pattern 触发）
- data/knowledge/facts.yaml     →  L0b 确定性事实（关键词匹配）
- data/knowledge/persona_lore   →  L0c 角色亲历（第一人称）
- data/knowledge/world_lore     →  L1 世界观（第三人称）

用法：
    from src.llm.knowledge import get_knowledge_service
    section = get_knowledge_service().section(user_text)   # 空串 = 不注入
"""

from __future__ import annotations

import threading
import time
from typing import Optional

from src.llm.knowledge.format import format_for_injection
from src.llm.knowledge.gate import KnowledgeGate
from src.llm.knowledge.loader import KnowledgeBase, load_knowledge
from src.llm.knowledge.recall import KnowledgeRecall
from src.utils import console

__all__ = [
    "KnowledgeBase",
    "KnowledgeGate",
    "KnowledgeRecall",
    "KnowledgeService",
    "load_knowledge",
    "format_for_injection",
    "get_knowledge_service",
]


class KnowledgeService:
    """知识库门面：懒加载数据 + 闸门判定 + 检索 + 格式化（进程内单例）。"""

    def __init__(self) -> None:
        self._gate: Optional[KnowledgeGate] = None
        self._recall: Optional[KnowledgeRecall] = None
        self._lock = threading.Lock()

    def _ensure_loaded(self) -> None:
        """首次调用时加载知识库（线程安全，只加载一次）。"""
        if self._gate is not None:
            return
        with self._lock:
            if self._gate is not None:
                return
            kb = load_knowledge()
            gate = KnowledgeGate()
            gate.register_entities([
                kw for fact in kb.facts for kw in fact.keywords
            ])
            self._gate = gate
            # 懒导入 + 注入 embedding 服务：精确/bigram 都未命中时做语义兜底；
            # 未配置或本地服务不可用时静默降级（行为与不启用一致）
            from src.llm.utils.embedding import SiliconFlowEmbeddingProvider
            self._recall = KnowledgeRecall(
                kb, embedding=SiliconFlowEmbeddingProvider())
            # 后台预热 lore 语义矩阵：构建早于首次语义补位查询，
            # 避免在对话路径上同步向量化（本地 118 段耗时数秒）
            self._recall.preheat_lore()

    def section(self, user_text: str, *, max_total_chars: int = 1200) -> str:
        """按用户消息返回应注入的知识段；无需注入或不可用时返回空串。"""
        self._ensure_loaded()
        if self._gate is None or self._recall is None:
            return ""
        t0 = time.perf_counter()
        if not self._gate.should_inject(user_text):
            # 耗时回显到控制中心工具日志栏，便于监控每轮知识开销
            console.dim(
                f"[知识库] 闸门未命中，不注入 "
                f"({(time.perf_counter() - t0) * 1000:.1f}ms)")
            return ""
        t1 = time.perf_counter()
        level = self._gate.level(user_text)
        t2 = time.perf_counter()
        recalled = self._recall.recall(user_text, level=level)
        t3 = time.perf_counter()
        text = format_for_injection(
            recalled,
            level=level,
            max_total_chars=max_total_chars,
            # 剧情意图（level 2）但检索无匹配资料 → fail-closed 防编造约束块；
            # 普通实体提及（level 1）不强制注入，避免每轮都带约束噪音
            fail_closed=level >= 2,
        )
        t4 = time.perf_counter()
        hits = " ".join(f"{k}{len(v)}" for k, v in recalled.items() if v) or "0"
        console.dim(
            f"[知识库] 判定={(t1 - t0) * 1000:.1f}ms 层级={level} "
            f"检索={(t3 - t2) * 1000:.1f}ms(命中{hits}) "
            f"格式化={(t4 - t3) * 1000:.2f}ms 共{(t4 - t0) * 1000:.1f}ms")
        return text


_service: Optional[KnowledgeService] = None
_service_lock = threading.Lock()


def get_knowledge_service() -> KnowledgeService:
    """进程内单例（知识数据懒加载，数据在进程生命周期内固定）。"""
    global _service
    if _service is None:
        with _service_lock:
            if _service is None:
                _service = KnowledgeService()
    return _service
