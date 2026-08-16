"""OpenAI 兼容客户端统一工厂：消除多模块重复构造。

各模块（llm_brain / agent / evolution / prompt_evo / skill_eval / lifecycle）
原先各自构建 OpenAI/AsyncOpenAI，超时、占位 key、None base_url 的处理散落
且不一致。统一入口后：
- api_key 为空时自动补 "not-needed" 占位（OpenAI SDK 要求非空）；
- base_url 为空串/None 时传 None（SDK 自动回退官方端点）；
- 超时 / max_retries 仍由调用方按场景显式指定，行为与历史实现完全一致。
"""

from __future__ import annotations

from typing import Optional


def _normalize_endpoint(api_key: str, base_url: str) -> tuple[str, Optional[str]]:
    """补全 SDK 必需的非空 api_key；base_url 空值归一为 None。"""
    return (api_key or "not-needed"), (base_url or None)


def get_openai_client(
    *,
    api_key: str,
    base_url: str = "",
    timeout: float = 120.0,
    max_retries: int = 2,
):
    """同步 OpenAI 客户端（对话主链路用；max_retries 默认 2，限流不空等）。"""
    from openai import OpenAI

    key, url = _normalize_endpoint(api_key, base_url)
    return OpenAI(api_key=key, base_url=url, timeout=timeout, max_retries=max_retries)


def get_async_openai_client(
    *,
    api_key: str,
    base_url: str = "",
    timeout: float = 60.0,
    max_retries: int = 2,
):
    """异步 AsyncOpenAI 客户端（后台任务 / 记忆判决 / 进化 / 技能评估等）。"""
    from openai import AsyncOpenAI

    key, url = _normalize_endpoint(api_key, base_url)
    return AsyncOpenAI(api_key=key, base_url=url, timeout=timeout, max_retries=max_retries)


def build_thinking_extra_body(enabled: bool) -> dict:
    """构造 thinking 差异化请求体（智谱/DeepSeek 等 OpenAI 兼容服务通用）。

    输出结构与历史实现完全一致；服务不支持该字段时由调用方捕获异常后
    降级为 extra_body=None 重试（llm_brain 已有该兜底路径）。
    """
    return {"thinking": {"type": "enabled" if enabled else "disabled"}}
