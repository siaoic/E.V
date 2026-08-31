"""ev.social.mcp_speak — 把 wake / proactive 的工具暴露给 LLM。

AI 通过工具调用这 4 个函数来「控制自己的发言节奏」：
  - set_wake_config: 设置潜水/唤醒策略
  - cancel_wake:     立刻取消潜水
  - request_speak:   主动申请一次发言
  - nudge_check:     查询当前契机状态（想自己评估时机时用）

注册方式（E.V 主项目本地工具目录，get_merged_tools 自动带出）：
    from ev.social.mcp_speak import install_speak_tools_at_startup
    install_speak_tools_at_startup()   # bootstrap 时调用，幂等
或外部 MCP manager（参考接口，尽力而为）：
    from ev.social.mcp_speak import register_speak_tools
    register_speak_tools(mcp_manager)
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("ev.social.mcp_speak")


def get_speak_tool_definitions() -> list:
    """返回所有工具定义（OpenAI function 格式）。"""
    try:
        from .wake import get_tool_definitions
        defs = get_tool_definitions()
        # 参考格式 {name, description, input_schema} → OpenAI function 格式
        out = []
        for d in defs:
            out.append({
                "type": "function",
                "function": {
                    "name": d["name"],
                    "description": d["description"],
                    "parameters": d.get("input_schema", {"type": "object"}),
                },
            })
        return out
    except Exception as e:
        logger.exception(f"[mcp_speak] failed to load tool defs: {e}")
        return []


async def handle_speak_tool_call(name: str, args: dict) -> Any:
    """处理 AI 调用的工具。"""
    try:
        from .wake import handle_tool_call
        return await handle_tool_call(name, args or {})
    except Exception as e:
        logger.exception(f"[mcp_speak] tool call {name} failed: {e}")
        return {"ok": False, "error": str(e)}


# ===== 主项目本地工具目录注册（幂等）=====

def install_local_tools() -> bool:
    """把 4 个发言工具注册进主项目本地工具目录（plugins/builtin/tools）。

    _ToolContext.tools.register 直接写模块级 _TOOL_CATALOG（与 index.py
    register(ctx) 同一写入路径），get_merged_tools 即可带出给 LLM；
    call_tool 走本地兜底执行（handle_speak_tool_call）。
    幂等：已注册的同名工具直接跳过。失败返回 False（不影响主程序）。
    """
    try:
        import plugins.builtin.tools as _tools_pkg

        defs = get_speak_tool_definitions()
        if not defs:
            return False

        from ev.agent.tool_registry import _expand_parameters

        ctx = _tools_pkg._ToolContext()
        for d in defs:
            fn = d["function"]
            if fn["name"] in _tools_pkg._TOOL_CATALOG:
                continue  # 幂等
            ctx.tools.register(
                name=fn["name"],
                description=fn["description"],
                parameters=_expand_parameters(fn.get("parameters") or {}),
                execute=lambda args, _n=fn["name"]: handle_speak_tool_call(_n, args),
                timeout=30.0,
                enabled_by="TOOL_SOCIAL_SPEAK_ENABLED",
            )
        logger.info(f"[mcp_speak] installed {len(defs)} speak tools into local catalog")
        return True
    except Exception as e:
        logger.debug(f"[mcp_speak] install_local_tools failed (non-fatal): {e}")
        return False


def install_speak_tools_at_startup() -> None:
    """bootstrap 时调用：优先本地工具目录，失败静默（不影响主程序）。"""
    if install_local_tools():
        return
    # 兜底：外部 MCP manager（尽力而为，参考接口）
    try:
        import asyncio as _aio
        from ev.kernel.slots import _get_runtime_mcp  # type: ignore
    except Exception:
        pass


def register_speak_tools(mcp_manager) -> None:
    """注册工具到外部 MCP manager（参考接口，尽力而为）。

    Args:
        mcp_manager: 带 register_tool(name=..., description=..., 
                     input_schema=..., handler=...) 的管理器
    """
    try:
        if hasattr(mcp_manager, "register_tool"):
            for d in get_speak_tool_definitions():
                fn = d["function"]
                mcp_manager.register_tool(
                    name=fn["name"],
                    description=fn["description"],
                    input_schema=fn.get("parameters") or {"type": "object"},
                    handler=lambda args, n=fn["name"]: handle_speak_tool_call(n, args),
                )
            logger.info("[mcp_speak] registered speak tools to external manager")
        else:
            logger.warning("[mcp_speak] mcp_manager has no register_tool, skip")
    except Exception as e:
        logger.exception(f"[mcp_speak] register failed: {e}")
