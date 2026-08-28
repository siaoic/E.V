"""LLM 适配器抽象基类：流式对话的标准契约。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import AsyncIterator, List, Optional, Tuple

from ev.adapter.base import BaseAdapter

# 流式协议：chat_stream 产出 (mode, text) 元组
#   - ("delta", text) : 当前累加文本（打字机流式实时显示）
#   - ("final", text) : 一个完整可播分段（送 TTS / 字幕 / 复读检测 / 事件）
LLMDeltaMode  = Tuple[str, str]  # mode="delta"
LLMFinalMode  = Tuple[str, str]  # mode="final"


class BaseLLMAdapter(BaseAdapter):
    """大模型对话适配器。

    本项目实现：src/llm/llm_brain.py 的 LLMBrain（组合了工具调用 / 深度
    思考 / 记忆注入）。切换模型只新增一个实现类，上层调用不变。
    """

    name: str = "llm"

    @abstractmethod
    def chat_stream(self, user_text: str, *, proactive: bool = False,
                    history: Optional[list] = None) -> AsyncIterator[Tuple[str, str]]:
        """流式对话：按 (mode, text) 协议逐步产出回复。

        协议：yield (mode, text)，mode ∈ {"delta", "final"}
          - ("delta", text): 打字机流式实时显示。每段 text 是当前累加 buffer；
            下游用 `text[printed_len:]` 增量打印，避免重复。
          - ("final", text): 一个完整可播分段。触发 TTS 入队 / 字幕推送 /
            复读检测 / 事件总线等副作用。

        上层消费者应至少处理两种模式；只关心完整段的下游（如 proactive / 插件
        context API）可只消费 final；只关心打字机显示的下游（如 CLI）可只消费 delta。

        Args:
            user_text: 用户输入（或主动对话决策文本）
            proactive: 是否以「内部自主行动指令」身份调用（不写历史）
            history: 可选历史快照；None = 用完整历史

        Yields:
            tuple[str, str]: (mode, text) 元组
        """

    @abstractmethod
    def push_turn_context(self, contexts: List[str]) -> None:
        """注入本轮系统提示背景信息（插件 / 记忆召回用）。"""

    @abstractmethod
    def reload_client(self) -> None:
        """重建 LLM 客户端（配置热更新后调用）。"""
