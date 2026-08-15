"""LLM 对话层：llm_brain / stream / proactive / skills / tools。

按职责拆分子包：
  - constants.py    常量定义
  - cleaners/       内容清洗（句子分割 / content / api 消息）
  - tools/          工具调用（解析 / 格式化 / 执行）
  - history/        历史管理（画像策略话术注入 / 摘要压缩；裁剪见 tool_message_utils.py）
  - client/         客户端独立工具（429 限流重试等；流式与 client 创建仍在 llm_brain）
  - utils/          杂项（2-gram 召回 / 实质内容检测）
"""
