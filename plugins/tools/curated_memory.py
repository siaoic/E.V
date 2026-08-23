"""L2 内建长期记忆工具（Hermes 式 memory 工具）：add / replace / remove / 批量。

管理 MEMORY.md / USER.md 纯文本长期记忆（稳定事实层，冻结快照注入 system
prompt）。与现有 remember_fact / forget_memory（memU 向量记忆，用户明确
指令触发）互补：本工具面向模型自主沉淀，按 action 分发到 CuratedMemoryStore。

同步文件实现丢线程池执行（asyncio.to_thread），不阻塞 asyncio 主循环
（与 memory_tools.py 的既有约定一致）。
"""

from __future__ import annotations

import asyncio
import json

from src.llm.memory.curated import get_curated_store

# 合法目标：memory = AI 自己的笔记；user = 对观众的认知
_VALID_TARGETS = ("memory", "user")
_VALID_ACTIONS = ("add", "replace", "remove")


def _store_result(result: dict) -> str:
    """把存储层 dict 序列化为 JSON 工具结果（保中文可读）。"""
    return json.dumps(result, ensure_ascii=False)


async def _memory_curated(
    action: str = "",
    target: str = "memory",
    content: str = "",
    old_text: str = "",
    operations: list | None = None,
) -> str:
    """memory 工具入口：单操作（action + content/old_text）或批量（operations）。

    批量 ops 原子应用、最终态才查字符上限，让模型一次调用腾空间 + 写入。
    """
    if target not in _VALID_TARGETS:
        return _store_result({
            "success": False,
            "error": f"非法 target '{target}'（可选：memory / user）。"})

    store = get_curated_store()

    # 批量路径：全部校验通过才应用（全有或全无）
    if operations:
        if not isinstance(operations, list):
            return _store_result({
                "success": False,
                "error": "operations 必须是 {action, content?, old_text?} 列表。"})
        result = await asyncio.to_thread(store.apply_batch, target, operations)
        return _store_result(result)

    # 单操作路径：先校验必填参数，再分发
    if action not in _VALID_ACTIONS:
        return _store_result({
            "success": False,
            "error": f"未知 action '{action}'（可选：add / replace / remove）。"})
    if action == "add" and not (content or "").strip():
        return _store_result({"success": False, "error": "add 需要 content。"})
    if action == "replace" and not (old_text or "").strip():
        return _store_result({"success": False, "error": "replace 需要 old_text。"})
    if action == "replace" and not (content or "").strip():
        return _store_result({
            "success": False, "error": "replace 需要 content（删除请用 remove）。"})
    if action == "remove" and not (old_text or "").strip():
        return _store_result({"success": False, "error": "remove 需要 old_text。"})

    if action == "add":
        result = await asyncio.to_thread(store.add, target, content or "")
    elif action == "replace":
        result = await asyncio.to_thread(
            store.replace, target, old_text or "", content or "")
    else:
        result = await asyncio.to_thread(store.remove, target, old_text or "")
    return _store_result(result)
