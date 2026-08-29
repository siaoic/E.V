"""会话摘要 / 蒸馏 / 整库整合等长方法。

与 store.py 相同：以「接受 ButlerAgent self 作为首参数」的模块级函数
形式存在，由 core.ButlerAgent 的同名方法转发。
"""

from __future__ import annotations

import asyncio

from ev.llm.client.factory import build_thinking_extra_body, get_async_openai_client
from ev.llm.utils.jsonutil import parse_json_array
from ev.utils import config, console

from tools.memory import memory

from ._prompts import (
    _SUMMARIZE_SYSTEM,
    _DISTILL_SYSTEM,
    _INTEGRATE_SYSTEM,
)
from .store import _pick_owner, _entry_user


async def _summarize_session(self, turns: list[dict]) -> str:
    """对会话生成一段中文摘要。"""
    text = self._turns_text(turns)
    if not text:
        return ""
    return await self._chat_text(_SUMMARIZE_SYSTEM, text)


async def _distill_session(self, turns: list[dict]) -> list[dict]:
    """把会话蒸馏为持久记忆条目列表（不自动写入）。

    每条按模型标注的 subject 归属（具体观众名/主播/AI），不再整批
    归为同一个用户——会话里主播、多个观众、AI 的事实各归各的角色。
    """
    text = self._turns_text(turns)
    if not text:
        return []
    entries = await self._chat_json(_DISTILL_SYSTEM, text)
    fallback = _pick_owner(turns, None)
    return [
        {
            "name": item["name"],
            "description": item["name"],
            "content": item["content"],
            "user": _entry_user(item, fallback),
        }
        for item in entries
    ]


async def _integrate_memories(
    self, files: list[dict], batch: int = 15
) -> list[dict] | None:
    """整合蒸馏整库记忆碎片（AI 自己蒸馏 + 删除旧条目的前置步骤）。

    与 distill_session（单次会话）不同：本方法一次性处理存量记忆库，
    合并同主题碎片、剔除近似重复，输出可直接 commit_recall_files 的新条目。
    任一批连续解析失败则返回 None（调用方据此保留原记忆、不删除）。

    用主模型（LLM_* 配置）单独建客户端：整库整合质量要求高（成功后会
    删除原碎片），BUTLER 管家模型（Qwen2.5-7B）曾出现 JSON 缺逗号/幻觉
    导致蒸馏失败的历史问题。要求单行紧凑 JSON（防 max_tokens 截断）。
    """
    if not files:
        return []
    base_url = (config.cfg.LLM_BASE_URL or "").strip()
    api_key = (config.cfg.LLM_API_KEY or "").strip()
    model = (config.cfg.LLM_MODEL or "").strip()
    if not (base_url and api_key and model):
        console.warn("[ButlerAgent] 记忆整合：主模型未配置，跳过")
        return None
    client = get_async_openai_client(api_key=api_key, base_url=base_url, timeout=60.0)
    recs: list[dict] = []
    batches = [files[i : i + batch] for i in range(0, len(files), batch)]
    for part in batches:
        # 该批输入中多数归属者作为回退（避免整合把具体观众名塌缩成默认用户）
        part_owners = [str(f.get("user") or memory._USER_DEFAULT) for f in part]
        fallback = (max(set(part_owners), key=part_owners.count)
                    if part_owners else memory._USER_DEFAULT)
        # 每条带 [归属者] 前缀，模型据此保留具体观众名/主播/AI 的独立归属
        user_text = "\n".join(
            f"{i + 1}. [{f.get('user') or '?'}] {f.get('content')}"
            for i, f in enumerate(part)
        )
        # 解析失败时带提示重试一次（常见于输出被截断 / JSON 语法错误）
        data: list | None = None
        for attempt in (1, 2):
            retry_hint = (
                "注意：上次输出无法解析为合法 JSON 数组，请只输出一个合法的 "
                "JSON 数组，元素间用逗号分隔，不要截断。"
                if attempt == 2
                else ""
            )
            _kw = dict(
                model=model,
                messages=[
                    {"role": "system", "content": _INTEGRATE_SYSTEM},
                    {
                        "role": "user",
                        "content": user_text + (retry_hint or ""),
                    },
                ],
                temperature=0.2,
                max_tokens=4096,
            )
            try:
                try:
                    # 显式关思考：整合输出 JSON 数组，思考只会拖慢整合
                    resp = await asyncio.wait_for(
                        client.chat.completions.create(
                            **_kw, extra_body=build_thinking_extra_body(False)),
                        timeout=90.0,
                    )
                except Exception:
                    # thinking 字段不被支持 → 降级普通模式重试
                    resp = await asyncio.wait_for(
                        client.chat.completions.create(**_kw), timeout=90.0)
            except Exception as e:
                console.warn(f"[ButlerAgent] 记忆整合调用失败：{e}")
                break
            content = parse_json_array(resp.choices[0].message.content or "")
            if content:
                break
        clean = [
            e
            for e in (content or [])
            if isinstance(e, dict) and e.get("name") and e.get("content")
        ]
        if not clean:
            console.warn("[ButlerAgent] 记忆整合某批失败，保留原记忆（不删除）")
            return None
        for e in clean:
            recs.append(
                {
                    "name": str(e["name"]).strip()[:64],
                    "description": str(e["name"]).strip()[:64],
                    "content": str(e["content"]).strip(),
                    "user": _entry_user(e, fallback),
                }
            )
    return recs
