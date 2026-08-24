"""memory 工具注册（L3-C）：纯文本长期记忆 add / replace / remove / 批量。"""

from plugins.builtin.tools.curated_memory import _memory_curated


def register(ctx):
    """注册 memory：模型自主沉淀稳定事实（MEMORY.md / USER.md）。"""
    ctx.tools.register(
        name="memory",
        description="把稳定事实保存到纯文本长期记忆（MEMORY.md / USER.md），"
                    "跨会话持久并在每轮对话注入系统提示，因此条目要短小精悍、"
                    "高价值。按 action 操作：add 新增一条；replace 用唯一子串"
                    "定位旧条目并替换（若匹配多条会报错，请用更具体的子串）；"
                    "remove 用唯一子串删除。建议用 operations 数组一次批量完成"
                    "「删旧的 + 写新的」（原子应用，最终态才查字符上限）。"
                    "何时用：用户透露稳定的偏好/习惯/个人信息、或你学到关于"
                    "观众/环境的持久事实时主动保存；不要存临时聊天内容。"
                    "target：memory = 你自己的笔记；user = 对观众的认知。",
        parameters={
            "action": {
                "type": "string",
                "enum": ["add", "replace", "remove"],
                "description": "单操作模式的动作；使用 operations 批量时省略",
            },
            "target": {
                "type": "string",
                "enum": ["memory", "user"],
                "description": "memory = AI 笔记；user = 观众认知（默认 memory）",
                "required": True,
            },
            "content": {
                "type": "string",
                "description": "条目内容（add/replace 必填）",
            },
            "old_text": {
                "type": "string",
                "description": "replace/remove 必填：定位旧条目的唯一子串",
            },
            "operations": {
                "type": "array",
                "description": "批量模式：一次应用多项操作（全有或全无）",
                "items": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["add", "replace", "remove"],
                        },
                        "content": {
                            "type": "string",
                            "description": "add/replace 的条目内容",
                        },
                        "old_text": {
                            "type": "string",
                            "description": "replace/remove 的定位子串",
                        },
                    },
                    "required": ["action"],
                },
            },
        },
        execute=lambda args: _memory_curated(**args),
        enabled_by="MEMORY_CURATED_ENABLED",
    )
