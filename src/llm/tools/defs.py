"""工具定义（OpenAI Function Calling 格式）—— 各工具与实现的对应关系见包 docstring。"""

from typing import List

_LOCAL_TOOL_DEFS: List[dict] = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "联网搜索网络内容并返回结果。当用户询问最新资讯、不知道的实时信息"
                           "或需要核实事实时使用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "想要搜索的关键词"},
                },
                "required": ["query"],
            },
        },
    },
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
                           "细节资源清单）。系统提示的 Available skills 段落只列出技能名"
                           "与描述，执行该类任务前必须先调用本工具获取完整指令。",
            "parameters": {
                "type": "object",
                "properties": {
                    "skill_name": {
                        "type": "string",
                        "description": "技能名，必须是 Available skills 段落中列出的精确名称",
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
]
