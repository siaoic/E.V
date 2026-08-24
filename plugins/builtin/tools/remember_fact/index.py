"""remember_fact 工具注册（L3-C）：保存长期记忆（固定记忆，不衰减）。"""

from plugins.builtin.tools.memory_tools import _remember_fact


def register(ctx):
    """注册 remember_fact：仅用户明确要求记住时调用。"""
    ctx.tools.register(
        name="remember_fact",
        description="把用户明确要求记住的信息保存为长期记忆（固定记忆，不受时间衰减影响）。"
                    "仅当用户给出明确的记忆指令（如「记住xxx」「帮我记住xxx」「一定要记住xxx」）"
                    "时调用；不要把普通聊天内容当成记忆写入。",
        parameters={
            "fact": {
                "type": "string",
                "description": "要记住的事实内容（用户原话或提炼后的完整事实）",
                "required": True,
            },
        },
        execute=lambda args: _remember_fact(**args),
        enabled_by="MEMORY_ENABLED",
    )
