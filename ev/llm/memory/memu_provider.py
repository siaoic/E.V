"""memU 内置记忆 Provider（L4 适配层，观察者模式）。

把现有 tools/memory/memory.py（memU 向量记忆）适配成 MemoryProvider 接口，
证明 L4 编排可跑通，并为未来接入外部语义后端提供模板。

设计要点（硬约束：默认行为 100% 不变）：
- 现有链路已直连 memU：llm_brain 每轮直接调用 memory.STANDING_INSTRUCTION /
  memory.retrieve()（召回）、ButlerAgent 每轮提取写入（sync）、
  remember_fact / forget_memory 工具由 plugins.builtin.tools 注册。
- 因此本 Provider 的 prefetch / sync_turn / get_tool_schemas 均 no-op
  （避免双召回、双写入、工具重复暴露），仅提供生命周期外壳；
- on_session_end 钩子预留给治理层（phase4 后台复盘），当前 no-op；
- 未来接外部后端：复制本文件改为真实实现，并注册进 MemoryManager 即可，
  上层编排（manager.py）无需改动。
"""

from __future__ import annotations

from typing import Any, Optional

from ev.utils import config
from ev.llm.memory.provider import MemoryProvider


class MemUProvider(MemoryProvider):
    """memU 向量记忆的 Provider 适配（观察现有直连调用链）。"""

    @property
    def name(self) -> str:
        return "memu"

    @property
    def is_builtin(self) -> bool:
        # memU 是项目内置适配层，不占"至多 1 个外部 Provider"名额
        return True

    def is_available(self) -> bool:
        """memU 是否启用：跟随 MEMORY_ENABLED 配置。"""
        return bool(getattr(config.cfg, "MEMORY_ENABLED", True))

    def system_prompt_block(self) -> str:
        # 现有链路已直连注入 STANDING_INSTRUCTION（llm_brain），避免重复
        return ""

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        # 现有链路已直连 memory.retrieve()（每轮召回），避免双召回
        return ""

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages: Optional[list[dict[str, Any]]] = None,
    ) -> None:
        # 现有 ButlerAgent 每轮从对话提取写入 memU，避免重复写入
        return None

    def get_tool_schemas(self) -> list[dict]:
        # remember_fact / forget_memory 由 plugins.builtin.tools 注册，无需重复暴露
        return []

    def on_session_end(self, messages: list[dict[str, Any]]) -> None:
        # 会话边界钩子：预留给治理层（phase4 后台复盘写 L2 MEMORY.md）
        return None

    def shutdown(self) -> None:
        # memU 图导出线程自管理，无需额外清理
        return None
