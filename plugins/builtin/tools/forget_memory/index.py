"""forget_memory 工具注册（L3-C）：按关键词删除长期记忆。"""

from plugins.builtin.tools.remember_fact.memory_tools import _forget_memory


def register(ctx):
    """注册 forget_memory：仅用户明确要求遗忘某事时调用。"""
    ctx.tools.register(
        name="forget_memory",
        description="删除与关键词相关的长期记忆。当用户明确要求遗忘某事"
                    "（如「忘掉xxx」「把xxx忘了」「别再记得xxx」）时调用；"
                    "若没有匹配到任何记忆，直接告诉用户没找到即可。",
        parameters={
            "keyword": {
                "type": "string",
                "description": "要遗忘的记忆关键词（记忆内容或名称包含它才会被删）",
                "required": True,
            },
        },
        execute=lambda args: _forget_memory(**args),
        enabled_by="MEMORY_ENABLED",
    )
