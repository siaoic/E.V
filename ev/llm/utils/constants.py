"""LLM 对话层常量定义（供 llm_brain 及各子模块共用）。"""

import os

from ev.utils import config

# 多轮工具调用上限（对标 live-2d(2) llm-handler.js 的 maxIterations=30）
_MAX_TOOL_ITERATIONS = 30

# 429 限流自动等待重试：总等待封顶（秒）。免费档服务端 1 并发限流，
# 高峰期常触发 429——按服务端头信息等待限流窗口结束后自动重试，
# 而不是直接中断对话；封顶后仍 429 才放弃。
_MAX_429_WAIT = 60.0

# 长对话摘要压缩：被裁剪的早期对话至少这么多条消息才值得压缩成摘要
_SUMMARIZE_MIN_TURNS = 4

# 工具执行结果日志截断长度（防刷屏；完整结果仍保留在工具上下文供模型使用）
_MAX_TOOL_RESULT_LOG = 300

# 跨轮历史中工具结果保留的最大长度（防 token 污染；本轮内仍用完整结果）
_MAX_TOOL_HISTORY_LOG = 500

# 生效话术建议文件：进化引擎写入（data/evolution_advice_active.json），
# 到期后由进化复盘回评续期/移除，这里只读取未到期条目注入系统提示
_ADVICE_ACTIVE_PATH = os.path.join(
    config.cfg.DATA_ROOT, "evolution_advice_active.json")

# 生效话术建议读取缓存时长（秒）：避免每轮对话都读文件
_ADVICE_CACHE_TTL = 30

# 观众画像文件：进化引擎复盘提炼（data/evolution_profile.json），
# 本模块每轮按关键词召回注入系统提示（对标 hermes 的 USER.md/MEMORY.md）
_PROFILE_PATH = os.path.join(
    config.cfg.DATA_ROOT, "evolution_profile.json")

# 画像读取缓存时长（秒）：避免每轮对话都读文件
_PROFILE_CACHE_TTL = 30

# 单轮最多注入的画像条数（按与当前消息相关度排序取前 N，控制 token）
_PROFILE_INJECT_MAX = 3

# GEPA 进化策略段文件：prompt_evo.py 择优落盘（data/evolution_policy.json），
# 本模块每轮读取注入系统提示（对标 hermes GEPA 的 prompt 进化层）
_POLICY_PATH = os.path.join(
    config.cfg.DATA_ROOT, "evolution_policy.json")

# 策略段读取缓存时长（秒）：避免每轮对话都读文件
_POLICY_CACHE_TTL = 30

# 工具结果进 LLM 上下文的总量上限（对标 llm-client.js _cleanMessagesForAPI 的
# MAX_CONTENT_LENGTH）：单条 8000 字符（cleaners/api.py），整轮累计 32K 字符；
# 跨轮：只保留摘要（_summarize_tool_content + _MAX_TOOL_HISTORY_LOG）——
# 防止 30 轮工具调用 × 8000 字符一次性进 LLM 上下文把免费档模型打爆。
_MAX_TOOL_CONTENT_LENGTH = 8000
_MAX_ROUND_TOOL_CHARS = 32000
