"""记忆管理工具：把「记住/忘掉」的判断交给 LLM 自己。

用户说「记住 xxx」「忘掉 xxx」时由主模型判断并调用本工具，而不是在
输入层用正则硬拦截。写入走 remember_explicit（固定记忆，pinned 不衰减 +
冲突自动替换），删除走 forget_phrase（按关键词匹配）。

实现注意：底层 memU 调用是同步桥接 async（_run_sync），在主线程事件循环
里直接调用会阻塞循环，因此统一丢到线程池执行（asyncio.to_thread）。

失败兜底：remember 失败时落本地失败队列（data/memory_retry.json），
下次启动时由 _drain_retry_queue 重放，LLM 感知是"暂存本地，重启后重试"
而不是直接报失败。
"""

import asyncio
import json
import os
import time

from src.utils import config
from tools.memory import memory

# 失败重试队列：写入失败的事实先落盘，进程启动时由 main 调 _drain_retry_queue。
# 避免 LLM 调 remember_fact 失败时只回一句 "AI 没记住" 草草了事。
_RETRY_QUEUE_PATH = os.path.join(config.cfg.PROJECT_ROOT, "data", "memory_retry.json")
_RETRY_QUEUE_MAX = 200  # 兜底条数：超过即截断最早的，防队列无限膨胀


def _append_retry_queue(kind: str, payload: dict | str) -> None:
    """落本地失败队列：restart 时由 _drain_retry_queue 重放。"""
    try:
        os.makedirs(os.path.dirname(_RETRY_QUEUE_PATH), exist_ok=True)
        try:
            with open(_RETRY_QUEUE_PATH, "r", encoding="utf-8") as f:
                items = json.load(f)
        except (OSError, ValueError):
            items = []
        if not isinstance(items, list):
            items = []
        item_payload = payload if isinstance(payload, dict) else {"content": payload}
        items.append({"kind": kind, "ts": time.time(), "payload": item_payload})
        if len(items) > _RETRY_QUEUE_MAX:
            items = items[-_RETRY_QUEUE_MAX:]
        tmp = _RETRY_QUEUE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False)
        os.replace(tmp, _RETRY_QUEUE_PATH)
    except Exception:
        pass  # 兜底写不进就算了，启动重放也能再补


def drain_retry_queue() -> int:
    """启动时重放失败队列：成功一条删一条，全失败保留。返回成功条数。

    由 main.py 启动后调一次；空队列 / 文件不存在直接返回 0。
    """
    try:
        with open(_RETRY_QUEUE_PATH, "r", encoding="utf-8") as f:
            items = json.load(f)
    except (OSError, ValueError):
        return 0
    if not isinstance(items, list) or not items:
        return 0
    remain: list = []
    success = 0
    for it in items:
        if not isinstance(it, dict):
            continue
        kind = it.get("kind")
        payload = it.get("payload") or {}
        try:
            if kind == "remember":
                content = str(payload.get("content") or "").strip()
                if not content:
                    continue
                memory.remember_explicit(content[:40], content)
                success += 1
                continue
            elif kind == "forget":
                kw = str(payload.get("keyword") or "").strip()
                if not kw:
                    continue
                memory.forget_phrase(kw)
                success += 1
                continue
        except Exception:
            pass
        # 没成功 / 未识别的 kind → 保留到下一轮
        remain.append(it)
    try:
        if remain:
            tmp = _RETRY_QUEUE_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(remain, f, ensure_ascii=False)
            os.replace(tmp, _RETRY_QUEUE_PATH)
        else:
            os.remove(_RETRY_QUEUE_PATH)
    except OSError:
        pass
    return success


async def _remember_fact(fact: str) -> str:
    """保存用户明确要求记住的事实（固定记忆，不受时间衰减影响）。

    写入失败时落本地失败队列，restart 时自动重试。
    """
    fact = (fact or "").strip()
    if not fact:
        return "错误：缺少要记住的内容。"
    # 长度上限：防 LLM 注入超长字符串（do_strip 后也会按 2000 截断）
    fact = fact[:2000]
    try:
        await asyncio.to_thread(memory.remember_explicit, fact[:40], fact)
        return f"已保存长期记忆：{fact}"
    except Exception as e:
        _append_retry_queue("remember", {"content": fact})
        return f"记忆暂存本地，AI 重启后会自动重试（{type(e).__name__}）"


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
        _append_retry_queue("forget", {"keyword": keyword})
        return f"遗忘暂存本地，AI 重启后会自动重试（{type(e).__name__}）"
