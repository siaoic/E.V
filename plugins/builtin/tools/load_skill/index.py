"""load_skill 工具注册（L3-C）：按名加载技能完整指令。"""

from plugins.builtin.tools.load_skill.skill_loader import _load_skill


def register(ctx):
    """注册 load_skill：先取 SKILL.md 全文再执行（渐进式披露）。"""
    ctx.tools.register(
        name="load_skill",
        description="加载指定技能的完整指令（SKILL.md 全文，并列出该技能捆绑的"
                    "细节资源清单）。系统提示的「可用技能」段列出各技能的触发时机，"
                    "情境匹配时先调用本工具获取完整指令再执行。",
        parameters={
            "skill_name": {
                "type": "string",
                "description": "技能名，必须是系统提示「可用技能」中列出的精确名称",
                "required": True,
            },
        },
        execute=lambda args: _load_skill(**args),
        enabled_by="TOOL_LOAD_SKILL_ENABLED",
    )
