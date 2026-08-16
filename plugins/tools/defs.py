"""工具定义（OpenAI Function Calling 格式）—— 各工具与实现的对应关系见包 docstring。"""

from typing import List

_LOCAL_TOOL_DEFS: List[dict] = [
    {
        "type": "function",
        "function": {
            "name": "get_current_time",
            "description": "用于获取当前日期和时间（含星期）。只有在想知道时间是什么时候才用。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "查询指定城市的当前天气。",
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {"type": "string", "description": "城市名，如：北京、上海"},
                },
                "required": ["city"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "load_skill",
            "description": "加载指定技能的完整指令（SKILL.md 全文，并列出该技能捆绑的"
                           "细节资源清单）。系统提示的「可用技能」段列出各技能的触发时机，"
                           "情境匹配时先调用本工具获取完整指令再执行。",
            "parameters": {
                "type": "object",
                "properties": {
                    "skill_name": {
                        "type": "string",
                        "description": "技能名，必须是系统提示「可用技能」中列出的精确名称",
                    },
                },
                "required": ["skill_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_skill_resource",
            "description": "按相对路径读取技能捆绑的细节资源（references/examples/scripts "
                           "目录下的文件，路径来自 load_skill 返回的资源清单）。SKILL.md "
                           "只含核心指令，需要详细证据、参考模式等细节时按需读取。",
            "parameters": {
                "type": "object",
                "properties": {
                    "skill_name": {
                        "type": "string",
                        "description": "技能名，与 load_skill 的 skill_name 一致",
                    },
                    "resource_path": {
                        "type": "string",
                        "description": "相对技能目录的路径，如 references/mental-models.md",
                    },
                },
                "required": ["skill_name", "resource_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "look_at_screen",
            "description": "截取当前电脑屏幕画面并用视觉模型观察，返回画面内容描述。"
                           "当用户问「你在看什么/你看到什么/我屏幕上有什么/能看到我吗」，"
                           "或需要了解当前屏幕、直播画面内容才能回答时调用。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remember_fact",
            "description": "把用户明确要求记住的信息保存为长期记忆（固定记忆，不受时间衰减影响）。"
                           "仅当用户给出明确的记忆指令（如「记住xxx」「帮我记住xxx」「一定要记住xxx」）"
                           "时调用；不要把普通聊天内容当成记忆写入。",
            "parameters": {
                "type": "object",
                "properties": {
                    "fact": {
                        "type": "string",
                        "description": "要记住的事实内容（用户原话或提炼后的完整事实）",
                    },
                },
                "required": ["fact"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "forget_memory",
            "description": "删除与关键词相关的长期记忆。当用户明确要求遗忘某事"
                           "（如「忘掉xxx」「把xxx忘了」「别再记得xxx」）时调用；"
                           "若没有匹配到任何记忆，直接告诉用户没找到即可。",
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {
                        "type": "string",
                        "description": "要遗忘的记忆关键词（记忆内容或名称包含它才会被删）",
                    },
                },
                "required": ["keyword"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "play_sound_effect",
            "description": "播放音效来增强对话的趣味性和表现力（如惊讶、爆炸、wow）。"
                           "想播音效时可先调用 list_sound_effects 查看可用音效，再按编号播放；"
                           "或直接用编号：01=搞啥情况, 02=突然一惊, 03=巨大爆炸, "
                           "04=钢管掉落, 05=OMG不可思议, 06=震撼管弦乐, 07=wow效果音。"
                           "播放有约 30% 概率失败，失败会返回提示，正常回应即可，不要反复重试。",
            "parameters": {
                "type": "object",
                "properties": {
                    "sfx_id": {
                        "type": "string",
                        "description": "音效编号（01-07），或逗号分隔的多个音效，如 '01,03'",
                    },
                    "repeat": {
                        "type": "integer",
                        "description": "连续播放次数（1-10），默认 1 次",
                        "minimum": 1,
                        "maximum": 10,
                        "default": 1,
                    },
                },
                "required": ["sfx_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_sound_effects",
            "description": "列出音效库中所有可用的音效编号与含义。"
                           "需要播放音效但不确定有哪些可选时调用。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]
