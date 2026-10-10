# TypeScript + Cordis 内核，A_Memorix 保留 Python 并经 RPC 接入

Status: 提议（设计阶段，待实现验证）

运行时采用 TypeScript / Node.js，以 Cordis 作为最小插件内核，负责插件注册、服务注入、作用域与生命周期清理。A_Memorix 暂不重写为 TypeScript，保留现有 Python 算法与存储，通过新增的本地 RPC 服务接入；记忆插件对 Cordis 暴露 `ctx.memory`，并负责管理独立的 Python 运行时。

代价：需要跨语言的进程管理；A_Memorix 必须先解耦 MaiBot 宿主依赖（如 `host_service.py`、`sdk_memory_kernel.py` 中的日志、配置、LLM 与 Embedding 调用），仅绕过宿主服务不够。
