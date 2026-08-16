"""Agent 任务执行系统（Firefly 风格 ReAct，默认不启用）。

对外入口：
- create_agent()：按配置构造 ReActAgent
- run_task()：一键执行任务（自动建连/关闭）

默认关闭（AGENT_ENABLED=false），仅在显式触发（!agent 命令）时启动；
高风险操作（shell 等）由沙箱门禁默认拒绝，AGENT_ALLOW_SHELL=true 才放行。
"""

from __future__ import annotations

from typing import Any, Optional

from src.utils import config
from src.agent.budget import TokenBudget
from src.agent.executor import ToolExecutor
from src.agent.loop import ReActAgent
from src.agent.sandbox import Sandbox
from src.agent.tools import build_builtin_tools

__all__ = [
    "ReActAgent",
    "Sandbox",
    "SandboxViolation",
    "ToolExecutor",
    "TokenBudget",
    "create_agent",
    "run_task",
]


def create_agent(cfg: Any = None, *, workspace: Optional[str] = None) -> ReActAgent:
    """按配置构造 Agent（OpenAI 兼容客户端 + 内置工具 + 沙箱）。"""
    cfg = cfg if cfg is not None else config.cfg
    from src.llm.client.factory import get_async_openai_client

    base_url = (cfg.LLM_BASE_URL or "").strip()
    api_key = (cfg.LLM_API_KEY or "").strip()
    model = (cfg.AGENT_MODEL or cfg.LLM_MODEL or "").strip()
    if not (base_url and api_key and model):
        raise ValueError("Agent 需要有效的 LLM 配置（LLM_BASE_URL / LLM_API_KEY / LLM_MODEL 或 AGENT_MODEL）")
    client = get_async_openai_client(api_key=api_key, base_url=base_url, timeout=60.0)
    root = workspace or cfg.AGENT_WORKSPACE or "."
    sandbox = Sandbox(root=str(root), allow_shell=bool(cfg.AGENT_ALLOW_SHELL))
    executor = ToolExecutor(build_builtin_tools(), sandbox)
    return ReActAgent(
        llm_client=client,
        llm_model=model,
        executor=executor,
        sandbox=sandbox,
        budget=TokenBudget(max_tokens=int(cfg.AGENT_MAX_TOKENS), model_name=model),
        max_steps=int(cfg.AGENT_MAX_STEPS),
    )


async def run_task(task: str, *, cfg: Any = None, progress_cb: Optional[Any] = None) -> str:
    """一键执行任务：构造 Agent → 运行 → 关闭客户端。"""
    agent = create_agent(cfg)
    try:
        if progress_cb is not None:
            agent.on_progress(progress_cb)
        return await agent.run(task)
    finally:
        await agent.close()
