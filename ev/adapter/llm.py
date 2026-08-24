"""LLM 适配器抽象基类：流式对话的标准契约。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import AsyncIterator, List, Optional

from ev.adapter.base import BaseAdapter


class BaseLLMAdapter(BaseAdapter):
    """大模型对话适配器。

    本项目实现：src/llm/llm_brain.py 的 LLMBrain（组合了工具调用 / 深度
    思考 / 记忆注入）。切换模型只新增一个实现类，上层调用不变。
    """

    name: str = "llm"

    @abstractmethod
    def chat_stream(self, user_text: str, *, proactive: bool = False,
                    history: Optional[list] = None) -> AsyncIterator[str]:
        """流式对话：逐句产出回复文本。

        Args:
            user_text: 用户输入（或主动对话决策文本）
            proactive: 是否以「内部自主行动指令」身份调用（不写历史）
            history: 可选历史快照；None = 用完整历史

        Yields:
            str: 逐段回复文本
        """

    @abstractmethod
    def push_turn_context(self, contexts: List[str]) -> None:
        """注入本轮系统提示背景信息（插件 / 记忆召回用）。"""

    @abstractmethod
    def reload_client(self) -> None:
        """重建 LLM 客户端（配置热更新后调用）。"""
