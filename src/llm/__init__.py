"""LLM 对话层：llm_brain / stream / proactive / skills / tools。

按职责拆分子包：
  - cleaners/       内容清洗（句子分割 / content / api 消息）
  - tools/          工具调用（解析 / 格式化 / 执行）
  - history/        历史管理（画像策略话术注入 / 摘要压缩；裁剪见 tool_message_utils.py）
  - client/         客户端独立工具（429 限流重试等；流式与 client 创建仍在 llm_brain）
  - utils/          杂项（常量 / JSON 容错解析 / 模型路由 / 2-gram 召回 /
                    实质内容检测 / 情绪分类 embedding）
  - evolution/      自我进化引擎（复盘 / 技能 / 话题 / GEPA 策略段 / 技能评估）
  - memory/         记忆整合（memU / 衰减 / 生命周期 / 防泄漏）
  - knowledge/      知识库（BM25 / 语义召回 / 门控）
"""
