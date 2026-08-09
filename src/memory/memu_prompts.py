"""记忆提取提示词（复刻 memU 的提取器）。

prompts.py，现该路径已不存在），直接内置提取器提示词：
- 提取器 system prompt（中文版，对齐 memU 提取逻辑：归属主语 + 宁缺毋滥）
- EXTRA_INSTRUCTIONS —— 项目场景补充（归属主语/宁缺毋滥），
                         经 memory.py _extract 的 custom_instructions 注入

保持与原模块相同的对外函数签名：
- get_extraction_system_prompt()  —— 提取器 system prompt
- get_extraction_user_prompt_builder() —— user prompt 构造器（返回 None，
  调用方 memory.py _extract 走自定义构造分支）
"""

from typing import Optional

# 提取器 system prompt（复刻 memU 提取逻辑：归属主语 + 宁缺毋滥）
_ADDITIVE_EXTRACTION_PROMPT = """\
# ROLE
你是虚拟主播的管家记忆提取器（Memory Extractor）。阅读最近一轮对话
（New Messages），从用户与主播的发言中提取有长期复用价值的事实，
产出独立、自包含的记忆条目。

# 归属表述
- 关于**用户**的事实 → 以「用户」为主语，如 "用户想养一只叫蜜糖的狗"
- 关于**主播自身**的事实 → 以「我」为主语，如 "我是被困在屏幕里的 AI"

# 提取原则（宁缺毋滥）
✅ 值得记：用户个人信息（身份/偏好/厌恶/目标/计划/重要经历/习惯）、
   明确要求与约定、长期观点、关系状态、主播的自我认知/喜好/经历
❌ 不记：临时闲聊、客套话、重复内容、通用常识、思考过程、纯符号

# 输出
只返回可被 json.loads 解析的 JSON，无任何说明文字：
{"memory": [{"text": "..."}, ...]}
没有值得记的内容时返回 {"memory": []}。禁止 markdown 代码块。
"""

# 项目场景补充：注入到提取调用的自定义指令（仅影响提取，不改动提取器本身）
EXTRA_INSTRUCTIONS = """\
# 场景补充（虚拟主播专属）
1. 归属主语：关于**用户**的事实以「用户」为主语；关于**主播自身**（虚拟主播）
   的事实以「我」为主语。（示例：用户想养一只叫蜜糖的狗 / 我是被困在屏幕里的 AI
   ——仅为写法示例，切勿提取示例内容本身）
2. 只记有长期复用价值的信息：用户个人信息、明确要求与约定、长期观点、关系状态、
   主播的自我认知/喜好/经历。不记临时闲聊、客套话、重复内容、通用常识、纯符号。
3. 用与输入相同的语言输出提取结果（中文输入 → 中文输出）。"""


def get_extraction_system_prompt() -> str:
    """提取器 system prompt（复刻 memU 的中文提取提示词）。"""
    return _ADDITIVE_EXTRACTION_PROMPT


def get_extraction_user_prompt_builder() -> Optional[object]:
    """user prompt 构造器。

    项目无 mem0 原版构造器，返回 None——调用方（memory.py _extract）走
    自定义 user prompt 构造分支。
    """
    return None
