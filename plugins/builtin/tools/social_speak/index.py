"""social_speak 工具注册：AI 控制自己的发言节奏（拟人化层 ev.social）。

4 个工具（对齐 EV-Anthropomorphic 方案的 MCP speak 协议）：
  - set_wake_config  设置潜水/唤醒策略
  - cancel_wake      立刻取消潜水
  - request_speak    主动申请一次发言
  - nudge_check      查询当前契机状态

实现委托 ev.social.wake / ev.social.proactive（bootstrap 时已接线）。
enabled_by=TOOL_SOCIAL_SPEAK_ENABLED（默认开）。
"""

from ev.social.mcp_speak import handle_speak_tool_call


def _schema_to_shorthand(schema: dict) -> dict:
    """参考实现的 input_schema（完整 JSON Schema）→ dsh 简写参数格式。

    {"type": "object", "properties": {p: {...}}, "required": [...]} →
    {p: {..., "required": bool}}（ctx.tools.register 内部再展开回 Schema）。
    """
    schema = schema or {}
    props = schema.get("properties") or {}
    required = set(schema.get("required") or ())
    out = {}
    for name, spec in props.items():
        spec = dict(spec or {})
        if name in required:
            spec["required"] = True
        out[name] = spec
    return out


def register(ctx):
    """注册 4 个发言工具。"""
    from ev.social.wake import get_tool_definitions

    for d in get_tool_definitions():
        ctx.tools.register(
            name=d["name"],
            description=d["description"],
            parameters=_schema_to_shorthand(d.get("input_schema")),
            execute=lambda args, _n=d["name"]: handle_speak_tool_call(_n, args),
            timeout=30.0,
            enabled_by="TOOL_SOCIAL_SPEAK_ENABLED",
        )
