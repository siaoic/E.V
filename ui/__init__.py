"""UI 包（项目根目录）：AI 桌宠控制中心 / 启动器 / 桌宠渲染。

- control_center.py + control_center.ui：控制中心（协调者，python -m ui.control_center）
- launcher.py：简单启动器（python -m ui.launcher）
- pet/：桌宠模式（PetWidget 渲染 + PetFaceDriver 驱动 + 定时器泵桥入口）
- widgets/、dialogs/、handlers/、pages/、filters/、utils/：控制中心拆分的
  可复用组件 / 弹窗 / 业务处理器 / 页面逻辑 / 事件过滤器 / 工具函数
"""
