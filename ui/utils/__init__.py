"""ui/utils：控制中心与启动器共用的纯工具函数（无 UI 依赖）。

- env_helpers：读写 .env（_update_env / _remove_env_key / _env_defaults）
- ansi_helpers：剥离主程序 stdout 的 ANSI 控制码
- path_helpers：frozen 项目根定位 / live2d 模型扫描
- constants：情绪、拖拽 MIME、插件配置字段、记忆层配色等常量
"""
