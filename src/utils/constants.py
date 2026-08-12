"""业务命名常量：集中管理跨模块复用的角色 / 来源标识。

避免魔法字符串散落各处导致拼写错误（如 "muika" 拼成 "muikka"）。
仅收纳语义易混、跨模块复用且改写收益高的标识；"user" / "assistant"
等语义自明且使用面极广的字符串不在此列。
"""

# 对话角色标识（写入记忆 recent_turns 的 role 字段）
ROLE_ASSISTANT = "assistant"  # AI 正式角色名
ROLE_AI_ALIAS = "muika"       # AI 别名（直播人格名），与 assistant 等价

# 记忆轮次来源标记（add_turn 的 source 参数）
SOURCE_DANMAKU_INPUT = "danmaku_input"  # 弹幕输入（观众发言）
SOURCE_DANMAKU_REPLY = "danmaku_reply"  # 弹幕回复（AI 回应弹幕）
SOURCE_PROACTIVE = "proactive"          # 主动对话（AI 自发发言）
