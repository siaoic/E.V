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
    """异步 AsyncOpenAI 客户端（后台任务 / 记忆判决 / 进化 / 技能评估等）。

    池化复用：同 (事件循环, base_url, api_key, 超时桶) 的调用共享同一实例与
    httpx 连接池，握手 -70%。调用方禁止 await client.close()（会关掉共享池），
    auxiliary 的 finally close 已删除。详见 pool.py。
    """
    from ev.llm.client.pool import get_pooled_async_client

    return get_pooled_async_client(
        api_key=api_key, base_url=base_url,
        timeout=timeout, max_retries=max_retries,
    )


def build_thinking_extra_body(enabled: bool) -> dict:
    """构造 thinking 差异化请求体（智谱/DeepSeek 等 OpenAI 兼容服务通用）。

    输出结构与历史实现完全一致；服务不支持该字段时由调用方捕获异常后
    降级为 extra_body=None 重试（llm_brain 已有该兜底路径）。

    DeepSeek v4 系支持 reasoning_effort（推理力度）参数：LLM_THINKING
    开启且 .env 配置了 LLM_REASONING_EFFORT（如 high）时随 body 下发；
    关闭思考或未配置时不传，保持对不认识该字段的服务零影响。
    """
    body: dict = {"thinking": {"type": "enabled" if enabled else "disabled"}}
    if enabled:
        from ev.utils import config  # 延迟导入，避免底层模块循环依赖
        effort = (getattr(config.cfg, "LLM_REASONING_EFFORT", "") or "").strip()
        if effort:
            body["reasoning_effort"] = effort
    return body


# D-7 优化：服务商 thinking 字段支持记忆（进程内）。首次探测到不支持
# （降级重试路径）后按「base_url|model」记住，后续调用直接省略该字段，
# 不再每次都白打一发必败请求（SiliconFlow 等不支持的服务每次省一个往返）。
_thinking_unsupported: "set[str]" = set()


def _thinking_key(base_url: str, model: str) -> str:
    return f"{(base_url or '').strip().rstrip('/')}|{(model or '').strip()}"


def thinking_is_supported(base_url: str, model: str) -> bool:
    """该 (服务, 模型) 组合是否已知支持 thinking 字段（未知视为支持）。"""
    return _thinking_key(base_url, model) not in _thinking_unsupported


def mark_thinking_unsupported(base_url: str, model: str) -> None:
    """探测到服务拒收 thinking 字段后调用（降级路径里记一次即可）。"""
    _thinking_unsupported.add(_thinking_key(base_url, model))
