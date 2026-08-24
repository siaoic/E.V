"""L4 治理管道（Hermes「会后秘书」）：后台复盘，把值得长期记住的事实写入 L2。

设计对齐 hermes 的 Flush Memories + 后台 Review Agent 双治理：
- run_curator()：从最近对话提取「稳定、长期有价值」的事实，写入
  MEMORY.md / USER.md（curated store 自动去重 + 威胁扫描 + 字符上限）；
- 冻结快照保证：复盘写入只影响下一次会话的 system prompt，当前会话
  的 prompt 完全不受影响（保 Prefix Cache 命中率）；
- 低频调度（每 MEMORY_CURATOR_INTERVAL 轮，由 llm_brain 触发），LLM
  调用走主 client 非流式小 max_tokens，失败静默不影响主链路。
"""

from __future__ import annotations

import asyncio
import json
import re

from ev.utils import console
from ev.llm.client.factory import build_thinking_extra_body
from ev.llm.memory.curated import get_curated_store

# 复盘提取只看最近对话（避免整库喂给 LLM，省 token 且聚焦近因事实）
_MAX_TRANSCRIPT_MESSAGES = 40
# 提取模型输出上限：只让模型挑最有价值的几条事实
_MAX_EXTRACTION_TOKENS = 512
# 单次复盘最多写入的条目数（防 LLM 一次倒灌太多）
_MAX_ENTRIES = 8


def build_curator_messages(history: list[dict]) -> list[dict]:
    """构造复盘提取用的 messages（system 指令 + 最近对话 transcript）。"""
    transcript = "\n".join(
        f"{m.get('role')}: {str(m.get('content') or '')[:500]}"
        for m in history[-_MAX_TRANSCRIPT_MESSAGES:]
        if m.get("content")
    )
    system = (
        "你是虚拟主播 E.V 的记忆秘书。回顾下面的对话，找出【值得长期记住】的"
        "稳定事实——观众的身份/偏好/经历、E.V 自己的长期目标/人设决定/约定。\n"
        "保守提取：只选不会很快过时的、对以后对话有价值的信息；"
        "不要把普通闲聊、寒暄、一次性请求、情绪化话语写进去。\n"
        "输出严格的 JSON 数组，每个元素为 {\"target\": \"memory\" 或 \"user\", "
        "\"content\": \"事实内容\"}。\n"
        "- memory：E.V 自己的笔记（目标、决定、风格约定等）；\n"
        "- user：对观众/用户的认知（身份、偏好、背景等）。\n"
        "没有值得记住的事实就输出 []，不要编造，不要输出 JSON 以外的任何内容。"
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": f"最近对话：\n{transcript}"},
    ]


def parse_extraction(text: str) -> list[dict]:
    """解析 LLM 输出的 JSON 数组为 [{target, content}]（容错剥代码块）。"""
    if not text:
        return []
    # 容错：剥 ```json ... ``` 包裹，以及首尾非 JSON 噪音
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.MULTILINE)
    start, end = raw.find("["), raw.rfind("]")
    if start == -1 or end == -1 or end <= start:
        return []
    try:
        data = json.loads(raw[start:end + 1])
    except (ValueError, TypeError):
        return []
    if not isinstance(data, list):
        return []
    entries: list[dict] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        target = str(item.get("target") or "memory")
        if target not in ("memory", "user"):
            target = "memory"
        content = str(item.get("content") or "").strip()
        if content:
            entries.append({"target": target, "content": content})
    return entries[:_MAX_ENTRIES]


def _call_extract(client, cfg, messages: list[dict]) -> str:
    """同步调用 LLM 提取（丢线程池执行；thinking 参数降级兼容）。"""
    kwargs = dict(
        model=cfg.LLM_MODEL,
        messages=messages,
        stream=False,
        max_tokens=_MAX_EXTRACTION_TOKENS,
        temperature=0.3,
    )
    try:
        resp = client.chat.completions.create(
            **kwargs, extra_body=build_thinking_extra_body(cfg.LLM_THINKING))
    except Exception:
        # 服务不支持 thinking 字段时降级重试（与 llm_brain 既有约定一致）
        resp = client.chat.completions.create(**kwargs)
    message = resp.choices[0].message
    return str(getattr(message, "content", None) or "")


async def run_curator(client, cfg, history: list[dict]) -> int:
    """后台复盘：从 history 提取事实写入 L2，返回实际写入条数。

    client 为 OpenAI 兼容 SDK 客户端（llm_brain 传入 self.client），
    cfg 为全局配置单例。任何一步失败都静默返回 0，不影响主链路。
    """
    if not history:
        return 0
    try:
        messages = build_curator_messages(history)
        text = await asyncio.to_thread(_call_extract, client, cfg, messages)
        entries = parse_extraction(text)
    except Exception as e:
        console.warn(f"[记忆复盘] LLM 提取失败：{e}")
        return 0
    if not entries:
        return 0

    store = get_curated_store()
    written = 0
    for target in ("memory", "user"):
        ops = [{"action": "add", "content": e["content"]}
               for e in entries if e["target"] == target]
        if not ops:
            continue
        try:
            result = await asyncio.to_thread(store.apply_batch, target, ops)
            if result.get("success"):
                written += len(ops)
        except Exception as e:
            console.warn(f"[记忆复盘] 写入 {target} 失败：{e}")
    return written
