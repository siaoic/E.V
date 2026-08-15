"""ui/pages：各页面逻辑（以 mixin 挂到 ControlCenter，页面初始化 + 事件处理）。

- launch_page：启动页（模式选择/模型切换/表情动作页联动）
- llm_page：LLM 配置页（LLM / Embedding / 管家模型回填）
- settings_page：设置页（开关 / TTS / STT / B站弹幕回填）
- face_page：表情与动作页（绑定区构建 / 库重建 / 试播）
- memory_page：记忆页（图谱渲染 / 详情与列表弹窗 / 删除）
- plugins_page：插件页（卡片列表 / 配置页）
- about_page：关于页（内容为 .ui 静态文本，无交互逻辑）
"""
