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
            "name": "memory",
            "description": "把稳定事实保存到纯文本长期记忆（MEMORY.md / USER.md），"
                           "跨会话持久并在每轮对话注入系统提示，因此条目要短小精悍、"
                           "高价值。按 action 操作：add 新增一条；replace 用唯一子串"
                           "定位旧条目并替换（若匹配多条会报错，请用更具体的子串）；"
                           "remove 用唯一子串删除。建议用 operations 数组一次批量完成"
                           "「删旧的 + 写新的」（原子应用，最终态才查字符上限）。"
                           "何时用：用户透露稳定的偏好/习惯/个人信息、或你学到关于"
                           "观众/环境的持久事实时主动保存；不要存临时聊天内容。"
                           "target：memory = 你自己的笔记；user = 对观众的认知。",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["add", "replace", "remove"],
                        "description": "单操作模式的动作；使用 operations 批量时省略",
                    },
                    "target": {
                        "type": "string",
                        "enum": ["memory", "user"],
                        "description": "memory = AI 笔记；user = 观众认知（默认 memory）",
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
                "required": ["target"],
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
                           "播放有约 30% 概率失败，失败会返回提示，正常回应即可，不要反复重试。"
                           "若想音效与叙述同步（讲到那一刻响起），不要调用本工具，"
                           "直接在叙述文本对应位置插入标记 {{sfx:编号}}（编号同上），"
                           "系统会在读到该处时自动播放，标记本身不会被念出。",
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
    {
        "type": "function",
        "function": {
            "name": "write_diary",
            "description": "基于当天的对话写一篇日记并保存（data/diary/YYYY-MM-DD.md，"
                           "当天已有日记会自动合并重写不丢内容）。当用户要求"
                           "「写日记」「记日记」「写今天的日记」「记录今天」时调用。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "session_search",
            "description": "精确检索历史会话消息（区别于记忆的模糊语义召回）。"
                           "当需要回忆「之前聊过什么/谁说过什么/某句话原话」这类"
                           "精确词、人名、梗时调用，返回匹配的历史消息 JSON。"
                           "mode：DISCOVER 按关键词搜索（默认）；READ 按消息 id 读单条；"
                           "SCROLL 按会话时间线翻页（配 session_id/before_ts）；"
                           "BROWSE 列出指定会话最近消息。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "搜索关键词（DISCOVER 必填；中文短词如人名、梗均可命中）",
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["DISCOVER", "READ", "SCROLL", "BROWSE"],
                        "description": "检索模式，默认 DISCOVER",
                    },
                    "session_id": {
                        "type": "string",
                        "description": "会话标识（SCROLL/BROWSE 用）",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "返回条数上限（1-50，默认 10）",
                    },
                    "before_ts": {
                        "type": "string",
                        "description": "时间线翻页锚点（SCROLL：只取早于此时间戳的消息）",
                    },
                    "msg_id": {
                        "type": "integer",
                        "description": "消息 id（READ 模式用）",
                    },
                },
            },
        },
    },
]
