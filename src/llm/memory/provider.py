"""L4 外部记忆 Provider 抽象（Hermes 式 memory_provider.py 精简落地）。

四层记忆架构的 L4 层：可插拔的语义记忆后端。本项目的内置适配是
MemUProvider（memu_provider.py，观察现有 tools/memory/memory 的直连
调用链，不重复召回/写入）；未来接入外部语义后端（向量库、云端记忆等）
只需实现本接口并注册进 MemoryManager，上层编排不变。

生命周期（由 MemoryManager 调度）：
- initialize()           会话启动时初始化（建资源/连后端）
- system_prompt_block()  静态段注入 system prompt
- prefetch()             每轮对话前召回相关上下文
- sync_turn()            每轮对话后异步写回
- get_tool_schemas()     暴露给模型的工具 schema
- handle_tool_call()     工具调用分发
- shutdown()             进程退出清理

边界钩子（可选覆盖）：
- on_session_end()      会话结束提取
- on_pre_compress()     上下文压缩前提取
- on_memory_write()     内建记忆工具写入时镜像
- backup_paths()        额外落盘路径声明

所有方法同步实现（内部自行处理阻塞），MemoryManager 用
asyncio.to_thread 丢线程池调用，不阻塞主事件循环。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Optional


class MemoryProvider(ABC):
    """可插拔记忆后端统一接口。所有实现必须保证接口一致。"""

    @property
    @abstractmethod
    def name(self) -> str:
        """短标识（如 'memu'、'honcho'），用于日志与路由。"""

    @property
    def is_builtin(self) -> bool:
        """是否内置 Provider（内置不占外部 provider 名额）。

        对标 Hermes memory_manager：1 个内置 + 至多 1 个外部 Provider。
        内置（如 memu 适配层）不受此限制，外部后端注册第 2 个会被拒绝。
        """
        return False

    @staticmethod
    def is_trivial_prompt(query: str, min_len: int = 4) -> bool:
        """是否无关紧要的召回 query（过短 / 纯语气 / 纯问候）。

        命中时调用方应跳过向量召回注入，节省 token（对标 Hermes
        memory_provider.is_trivial_prompt）。中文场景 4 字以内的消息
        通常没有值得检索的实体/主题（"哈哈""早上好"），召回结果多为噪声。
        """
        return not query or len(query.strip()) < min_len

    # -- 核心生命周期（必须实现） --------------------------------------------

    @abstractmethod
    def is_available(self) -> bool:
        """后端是否已配置且可用（仅检查配置/依赖，不做网络调用）。

        返回 False 时 MemoryManager 不激活该 Provider。
        """

    def initialize(self, session_id: str, **kwargs) -> None:
        """会话启动时初始化（连接、预热）。kwargs 可含 agent_context 等。"""

    def system_prompt_block(self) -> str:
        """返回注入 system prompt 的静态段；无则返回空串。

        动态召回上下文由 prefetch() 注入，不在此处。
        """
        return ""

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """每轮对话前召回与 query 相关的上下文，返回注入文本（空串则跳过）。

        实现应保证快速——慢后端应在后台线程召回并在此返回缓存结果。
        """
        return ""

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages: Optional[list[dict[str, Any]]] = None,
    ) -> None:
        """每轮对话后把完成的轮次写回后端。

        messages 为截至本轮的工具链消息列表，不需要原始轮次上下文的后端
        可以忽略。实现应非阻塞（后台队列）。
        """

    def get_tool_schemas(self) -> list[dict]:
        """返回本 Provider 暴露的工具 schema（OpenAI function 格式）。

        无工具（仅上下文）返回空列表。
        """
        return []

    def handle_tool_call(self, tool_name: str, args: dict, **kwargs) -> str:
        """处理本 Provider 工具调用，返回 JSON 字符串结果。

        仅对 get_tool_schemas() 返回的工具名被调用。
        """
        raise NotImplementedError(
            f"Provider {self.name} 不处理工具 {tool_name}")

    def shutdown(self) -> None:
        """进程退出清理（刷队列、关连接）。"""

    # -- 边界钩子（覆盖以启用） ----------------------------------------------

    def on_session_end(self, messages: list[dict[str, Any]]) -> None:
        """会话结束时调用（非每轮），用于事实提取/总结。

        messages 为完整会话消息列表。
        """

    def on_pre_compress(self, messages: list[dict[str, Any]]) -> str:
        """上下文压缩丢弃旧消息前调用，提取将被压缩的信息。

        返回要并入压缩摘要提示的文本；无贡献返回空串。
        """
        return ""

    def on_memory_write(
        self,
        action: str,
        target: str,
        content: str,
        metadata: Optional[dict[str, Any]] = None,
    ) -> None:
        """内建记忆工具（L2 curated memory）写入时镜像到本后端。

        action: add / replace / remove；target: memory / user。
        默认 no-op——镜像写入会改变后端内容，需显式实现才启用。
        """

    def backup_paths(self) -> list[str]:
        """返回本 Provider 存储在本项目 DATA_ROOT 之外的落盘路径。

        供备份工具收集；都在 DATA_ROOT 内的后端返回空列表。
        """
        return []
