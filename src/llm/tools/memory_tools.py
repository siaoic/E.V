"""记忆管理工具：把「记住/忘掉」的判断交给 LLM 自己。

用户说「记住 xxx」「忘掉 xxx」时由主模型判断并调用本工具，而不是在
输入层用正则硬拦截。写入走 remember_explicit（固定记忆，pinned 不衰减 +
冲突自动替换），删除走 forget_phrase（按关键词匹配）。

实现注意：底层 memU 调用是同步桥接 async（_run_sync），在主线程事件循环
里直接调用会阻塞循环，因此统一丢到线程池执行（asyncio.to_thread）。
"""

import asyncio

from src.memory import memory


async def _remember_fact(fact: str) -> str:
    """保存用户明确要求记住的事实（固定记忆，不受时间衰减影响）。"""
    fact = (fact or "").strip()
    if not fact:
        return "错误：缺少要记住的内容。"
    try:
        await asyncio.to_thread(memory.remember_explicit, fact[:40], fact)
        return f"已保存长期记忆：{fact}"
    except Exception as e:
        return f"记忆保存失败：{type(e).__name__}: {e}"


async def _forget_memory(keyword: str) -> str:
    """删除与关键词相关的长期记忆（无匹配时明说没删）。"""
    keyword = (keyword or "").strip()
    if not keyword:
        return "错误：缺少要遗忘的关键词。"
    try:
        deleted = await asyncio.to_thread(memory.forget_phrase, keyword)
        if deleted:
            return f"已删除 {deleted} 条与「{keyword}」相关的记忆"
        return f"没有找到与「{keyword}」相关的记忆，什么也没删"
    except Exception as e:
        return f"记忆删除失败：{type(e).__name__}: {e}"
