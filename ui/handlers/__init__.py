"""ui/handlers：业务处理器（以 mixin 挂到 ControlCenter）。

- process_handler：主进程管理（启动/停止/日志/stdin）
- config_handler：配置读写（.env / 人设文件 / 保存反馈）
- service_handler：外部服务进程托管（mindcraft 等）
- plugin_handler：插件卡片数据与启停逻辑
- face_handler：表情/动作绑定映射逻辑
"""
