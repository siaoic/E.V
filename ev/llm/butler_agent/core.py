"""ButlerAgent 类骨架：客户端 helper + 视觉描述 + 主动发言构造。

长方法（提取/蒸馏/整合）分散到 store.py 与 summarize.py，
通过继承或 monkey-patch 的方式「拼回」完整类，保持行为等价。
实际实现：核心方法在本文件，其余用 mixin / import 直接挂到 ButlerAgent 上。
为避免多继承复杂度，这里实现完整 ButlerAgent，store/summarize 模块提供
纯函数作为内部调用目标——但保证调用 0 改动的简单方式是：所有方法都在
本文件的 ButlerAgent 类定义内，长方法调其它模块的函数。

更稳的实现：所有方法都在这里定义，但把方法体的大段实现委派给 store / summarize
模块的独立函数。这样「方法还是类的方法」，import 语义零差异，方法行数可拆。
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from openai import AsyncOpenAI

from ev.llm.client.factory import (
    build_thinking_extra_body,
    get_async_openai_client,
    mark_thinking_unsupported,
    thinking_is_supported,
)
from ev.llm.utils.jsonutil import parse_json_array
from ev.utils import config, console

from tools.memory import memory

from ._prompts import (
    _PROACTIVE_PROMPT_TEMPLATE,
    _EXTRACT_WORKERS,
    _period_phrase,
)
from . import store as _store_mod
from . import summarize as _summarize_mod


class ButlerAgent:
    """记忆提取 / 会话摘要 / 蒸馏 / 主动发言构造。"""

    def __init__(self) -> None:
        self._client: AsyncOpenAI | None = None
        self._model = ""
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=16)
        self._worker_tasks: list[asyncio.Task] = []
        # 实时强信号捕获：待入库条目 + 后台消费任务（合并小批写入）
        self._instant_pending: list[dict] = []
        self._instant_task: asyncio.Task | None = None

    # ---------- 客户端 ----------

    def _ensure_client(self) -> AsyncOpenAI | None:
        """按 BUTLER_*（留空回退 LLM_*）构建 OpenAI 兼容客户端。"""
        if self._client is None:
            base_url = config.cfg.BUTLER_BASE_URL or config.cfg.LLM_BASE_URL
            api_key = config.cfg.BUTLER_API_KEY or config.cfg.LLM_API_KEY
            self._model = config.cfg.BUTLER_MODEL or config.cfg.LLM_MODEL
            if not (base_url and api_key and self._model):
                return None
            # 明确超时：记忆提取/摘要等后台任务不设限会无限等待（agent 后台
            # 静默卡死），超时后放弃本次调用由调用方静默跳过
            self._client = get_async_openai_client(
                api_key=api_key, base_url=base_url, timeout=45.0)
        return self._client

    async def _complete(self, messages: list[dict], temperature: float):
        """调用管家模型：默认关闭思考（提取要干净 JSON，不被推理文本污染）。

        D-7 优化：thinking 支持记忆（factory 共享，进程内）——已知不支持
        的 (服务, 模型) 直接省略 extra_body，不再每次都打一发必败请求
        等 400 再降级；未知服务仍首次探测，降级时记住结果。
        失败返回 None 由调用方静默跳过。
        """
        client = self._ensure_client()
        if client is None:
            return None
        base_url = config.cfg.BUTLER_BASE_URL or config.cfg.LLM_BASE_URL
        use_thinking = thinking_is_supported(base_url, self._model)
        try:
            try:
                kwargs: dict = dict(
                    model=self._model,
                    messages=messages,
                    temperature=temperature,
                )
                if use_thinking:
                    kwargs["extra_body"] = build_thinking_extra_body(False)
                return await asyncio.wait_for(
                    client.chat.completions.create(**kwargs),
                    timeout=45.0,
                )
            except Exception as e:
                if not use_thinking or "429" in str(e):
                    raise  # 已降级过 / 限流不是 thinking 字段问题，不重复降级
                console.dim("[ButlerAgent] 服务不支持 thinking 参数，降级为普通模式")
                mark_thinking_unsupported(base_url, self._model)
                return await asyncio.wait_for(
                    client.chat.completions.create(
                        model=self._model, messages=messages, temperature=temperature
                    ),
                    timeout=45.0,
                )
        except Exception as e:
            console.warn(f"[ButlerAgent] 模型调用失败：{e}")
            return None

    @staticmethod
    def _message_text(message) -> str:
        """取模型回复正文：部分推理模型 content 为空、正文在 reasoning_content，
        两者都读，保证提取/摘要不因空 content 而丢失（对标 llm_brain 的兜底）。"""
        content = (getattr(message, "content", None) or "").strip()
        if not content:
            content = (getattr(message, "reasoning_content", None) or "").strip()
        return content

    async def _chat_json(self, system: str, user_text: str) -> list[dict]:
        """调用管家模型并解析 JSON 数组结果（解析失败返回空列表）。"""
        response = await self._complete(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": user_text},
            ],
            temperature=0.2,
        )
        if response is None:
            return []
        content = self._message_text(response.choices[0].message)
        data = parse_json_array(content)
        if data is None:
            return []
        return [
            {
                "name": str(item.get("name") or "").strip()[:64],
                "content": str(item.get("content") or "").strip(),
                "subject": str(item.get("subject") or "").strip()[:64],
                "subject_type": str(item.get("subject_type") or "").strip()[:24],
                "predicate": str(item.get("predicate") or "").strip()[:64],
                "object": str(item.get("object") or "").strip()[:64],
                "object_type": str(item.get("object_type") or "").strip()[:24],
                "owner": str(item.get("owner") or "").strip().lower()[:8],
            }
            for item in data
            if isinstance(item, dict) and (item.get("name") or item.get("content"))
        ]

    async def _chat_text(self, system: str, user_text: str) -> str:
        """调用管家模型并返回纯文本结果。"""
        response = await self._complete(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": user_text},
            ],
            temperature=0.4,
        )
        if response is None:
            return ""
        return self._message_text(response.choices[0].message).strip()

    @staticmethod
    def _turns_text(turns: list[dict] | None) -> str:
        """把对话轮次列表格式化为文本（区分弹幕/主播/AI 三方角色）。"""
        return memory.format_turns_text(turns)

    # ---------- 记忆提取（memU agentic 写入链路）----------
    # 方法体委派到 store 模块的实现（逐字移动，不改逻辑）

    async def submit_extract_and_store(
        self, new_turns: list[dict], recent_turns: list[dict] | None
    ) -> None:
        await _store_mod._submit_extract_and_store(self, new_turns, recent_turns)

    async def _run_worker(self) -> None:
        await _store_mod._run_worker(self)

    async def extract_and_store(
        self, new_turns: list[dict], recent_turns: list[dict] | None
    ) -> None:
        await _store_mod._extract_and_store(self, new_turns, recent_turns)

    def _submit_instant_capture(self, new_turns: list[dict]) -> None:
        _store_mod._submit_instant_capture(self, new_turns)

    async def _commit_instant(self) -> None:
        await _store_mod._commit_instant(self)

    # ---------- 会话摘要与蒸馏 ----------
    # 方法体委派到 summarize 模块的实现

    async def summarize_session(self, turns: list[dict]) -> str:
        return await _summarize_mod._summarize_session(self, turns)

    async def distill_session(self, turns: list[dict]) -> list[dict]:
        return await _summarize_mod._distill_session(self, turns)

    async def integrate_memories(
        self, files: list[dict], batch: int = 15
    ) -> list[dict] | None:
        return await _summarize_mod._integrate_memories(self, files, batch)

    # ---------- 视觉描述（图片直接交给 agent 处理） ----------

    async def describe_image(
        self, image_b64: str, prompt: str = "", max_tokens: int = 1024,
    ) -> str:
        """把图片（base64）交给视觉模型描述，返回描述文本。"""
        prompt = prompt or "用简洁自然的中文描述这张图片的内容。"
        candidates = self._vision_candidates()
        if not candidates:
            console.warn("[ButlerAgent] 视觉模型未配置，无法描述图片")
            return ""
        for base_url, api_key, model in candidates:
            client = get_async_openai_client(
                api_key=api_key, base_url=base_url, timeout=60.0)
            use_thinking = thinking_is_supported(base_url, model)
            kwargs: dict = dict(
                model=model,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/jpeg;base64,{image_b64}"
                                },
                            },
                        ],
                    }
                ],
                max_tokens=max_tokens,
                temperature=0.4,
            )
            if use_thinking:
                kwargs["extra_body"] = build_thinking_extra_body(False)
            try:
                try:
                    resp = await asyncio.wait_for(
                        client.chat.completions.create(**kwargs),
                        timeout=60.0,
                    )
                except Exception as e:
                    if not use_thinking or "429" in str(e):
                        raise  # 已降级过 / 限流不是 thinking 字段问题
                    console.dim(
                        "[ButlerAgent] 视觉服务不支持 thinking 参数，降级为普通模式")
                    mark_thinking_unsupported(base_url, model)
                    kwargs.pop("extra_body", None)
                    resp = await asyncio.wait_for(
                        client.chat.completions.create(**kwargs),
                        timeout=60.0,
                    )
            except Exception as e:
                console.warn(
                    f"[ButlerAgent] 视觉模型 {model} 调用失败，尝试下一候选：{e}")
                continue
            return (resp.choices[0].message.content or "").strip()
        console.warn("[ButlerAgent] 视觉模型全部调用失败，无法描述图片")
        return ""

    @staticmethod
    def _vision_candidates() -> list[tuple[str, str, str]]:
        """视觉候选服务列表：主模型（LLM_*）优先，主模型不支持图片时
        依次回退 BUTLER_*（默认 glm-4v-flash），去重相同服务。"""
        candidates: list[tuple[str, str, str]] = []
        main = (
            (config.cfg.LLM_BASE_URL or "").strip(),
            (config.cfg.LLM_API_KEY or "").strip(),
            (config.cfg.LLM_MODEL or "").strip(),
        )
        if all(main):
            candidates.append(main)
        fallback = (
            (config.cfg.BUTLER_BASE_URL or config.cfg.LLM_BASE_URL or "").strip(),
            (config.cfg.BUTLER_API_KEY or config.cfg.LLM_API_KEY or "").strip(),
            (config.cfg.BUTLER_MODEL or "glm-4v-flash").strip(),
        )
        if all(fallback) and fallback not in candidates:
            candidates.append(fallback)
        return candidates

    # ---------- 主动发言构造 ----------

    def build_proactive_prompt(
        self, topic: str = "", memory_context: str = "", hour: int | None = None
    ) -> str:
        """构造主动发言请求（LLM 自主决定：想说就说，不想说保持沉默）。"""
        hour = hour if hour is not None else datetime.now().hour
        period = _period_phrase(hour)
        memory_hint = f"\n相关记忆线索：\n{memory_context}" if memory_context else ""
        topic_hint = (f"可以顺着这个灵感话题聊：{topic}"
                      if topic else "也可以自己决定想聊什么")
        return _PROACTIVE_PROMPT_TEMPLATE.format(
            period=period, topic_hint=topic_hint, memory_hint=memory_hint)
