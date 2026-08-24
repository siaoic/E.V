"""read_skill_resource 工具注册（L3-C）：按相对路径读取技能捆绑资源。"""

from plugins.builtin.tools.load_skill.skill_loader import _read_skill_resource


def register(ctx):
    """注册 read_skill_resource：按需读 references/examples/scripts 下细节文件。"""
    ctx.tools.register(
        name="read_skill_resource",
        description="按相对路径读取技能捆绑的细节资源（references/examples/scripts "
                    "目录下的文件，路径来自 load_skill 返回的资源清单）。SKILL.md "
                    "只含核心指令，需要详细证据、参考模式等细节时按需读取。",
        parameters={
            "skill_name": {
                "type": "string",
                "description": "技能名，与 load_skill 的 skill_name 一致",
                "required": True,
            },
            "resource_path": {
                "type": "string",
                "description": "相对技能目录的路径，如 references/mental-models.md",
                "required": True,
            },
        },
        execute=lambda args: _read_skill_resource(**args),
        enabled_by="TOOL_LOAD_SKILL_ENABLED",
    )
