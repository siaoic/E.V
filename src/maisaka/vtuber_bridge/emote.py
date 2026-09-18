# -*- coding: utf-8 -*-
"""表情出口：``vtuber_emote`` 工具 + 表情定时关闭服务。

按实施计划 §4-D7：表情工具用**独立 ``ToolProvider``**（与 WorldToolProvider
同层）注册，**不注册成「世界」**——包络源与注入都在主程序进程内，
注册成世界反而套上一层不可用的插件回呼契约。

表情清单来自 ``[vtuber].expressions``（中文名 → VTS 表情文件名）。
连接建立后会用 VTS 实际表情列表校验配置；调用时表情不在配置表内
直接返回错误，配置了但模型上不存在的表情由 VTS 报错并明确回传。
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Dict, Optional

from src.core.tooling import (
    ToolAvailabilityContext,
    ToolExecutionContext,
    ToolExecutionResult,
    ToolInvocation,
    ToolSpec,
)

from . import state

EMOTE_TOOL_NAME = "vtuber_emote"


class EmoteService:
    """表情触发与定时关闭。"""

    def __init__(self, *, client, expressions: Dict[str, str], hold_seconds: float, logger) -> None:
        """构建服务。

        Args:
            expressions: 中文名 → VTS 表情文件名映射（来自配置）。
            hold_seconds: 表情默认保持时长，到时自动关闭。
        """

        self._client = client
        self._expressions = dict(expressions)
        self._hold_seconds = float(hold_seconds)
        self._logger = logger
        self._close_tasks: Dict[str, asyncio.Task[None]] = {}

    @property
    def configured_emotions(self) -> list[str]:
        """配置里可用的中文名列表（供工具描述动态生成）。"""
        return list(self._expressions.keys())

    def validate_against_model(self, available_files: list[str]) -> list[str]:
        """把配置表情与模型实际表情对照，返回不匹配的中文名列表。"""
        available = {name.casefold() for name in available_files}
        mismatched: list[str] = []
        for name_zh, file_name in self._expressions.items():
            candidates = {str(file_name).casefold(), f"{file_name}.exp3.json".casefold()}
            if not candidates & available:
                mismatched.append(name_zh)
        return mismatched

    async def activate(self, emotion: str) -> str:
        """触发一个表情并安排定时关闭。

        Returns:
            str: 结果描述文本（供工具返回）。

        Raises:
            ValueError: 表情不在配置表内。
            RuntimeError: VTS 未连接或注入失败。
        """
        name_zh = str(emotion or "").strip()
        file_name = self._expressions.get(name_zh)
        if not file_name:
            available = "、".join(self._expressions.keys()) or "（未配置任何表情）"
            raise ValueError(f"未知表情「{name_zh}」，可用表情：{available}")

        await self._client.set_expression(str(file_name), active=True, fade_time_sec=0.25)
        state.record_emote(name_zh, str(file_name), self._hold_seconds)
        self._schedule_close(name_zh, str(file_name))
        return f"已做出表情：{name_zh}（{self._hold_seconds:g}s 后自动恢复）"

    def _schedule_close(self, name_zh: str, file_name: str) -> None:
        """安排到时关闭；重复触发同一表情时重置计时。"""
        existing = self._close_tasks.pop(name_zh, None)
        if existing is not None and not existing.done():
            existing.cancel()

        async def close_later():
            try:
                await asyncio.sleep(self._hold_seconds)
                await self._client.set_expression(file_name, active=False, fade_time_sec=0.5)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 关闭失败记日志，不影响下一次触发
                if self._logger is not None:
                    self._logger.error(f"关闭表情 {name_zh} 失败: {exc!r}")

        self._close_tasks[name_zh] = asyncio.create_task(close_later(), name=f"vtuber-emote-{name_zh}")

    async def close_all(self) -> None:
        """停止全部定时关闭任务，并把已开启的表情关掉。"""
        for task in self._close_tasks.values():
            task.cancel()
        tasks = list(self._close_tasks.values())
        self._close_tasks.clear()
        await asyncio.gather(*tasks, return_exceptions=True)
        for name_zh, file_name in self._expressions.items():
            with contextlib.suppress(Exception):
                await self._client.set_expression(str(file_name), active=False, fade_time_sec=0.25)


class EmoteToolProvider:
    """把 ``vtuber_emote`` 工具注册进统一工具注册表。

    工具只在「口型桥已启用、VTS 已连接」时暴露；未就绪时 ``list_tools``
    返回空列表（工具不存在），调用兜底返回明确错误。
    """

    provider_name = "vtuber_bridge"
    provider_type = "vtuber_bridge"

    def __init__(self, service_provider=...) -> None:
        """初始化 Provider。

        Args:
            service_provider: 返回 :class:`EmoteService` 或 ``None`` 的回调，
                默认取全局服务的当前实例。
        """
        if service_provider is ...:
            from .service import get_emote_service

            service_provider = get_emote_service
        self._service_provider = service_provider

    def _service(self) -> Optional[EmoteService]:
        try:
            return self._service_provider()
        except Exception:
            return None

    async def list_tools(
        self,
        context: Optional[ToolAvailabilityContext] = None,
    ) -> list[ToolSpec]:
        """列出表情工具；未启用或未连接时返回空列表。"""
        del context
        service = self._service()
        if service is None or not state.is_connected():
            return []

        emotions = "、".join(service.configured_emotions)
        description = f"让 Live2D 模型做一个表情（保持约 {service._hold_seconds:g} 秒后自动恢复）。"
        if emotions:
            description += f"可用表情：{emotions}。"
        return [
            ToolSpec(
                name=EMOTE_TOOL_NAME,
                description=description,
                title="做表情",
                parameters_schema={
                    "type": "object",
                    "properties": {
                        "emotion": {
                            "type": "string",
                            "description": "表情中文名，必须从可用表情中选择",
                        }
                    },
                    "required": ["emotion"],
                },
                provider_name=self.provider_name,
                provider_type=self.provider_type,
            )
        ]

    async def invoke(
        self,
        invocation: ToolInvocation,
        context: Optional[ToolExecutionContext] = None,
    ) -> ToolExecutionResult:
        """执行表情工具调用。"""
        del context
        if invocation.tool_name != EMOTE_TOOL_NAME:
            return ToolExecutionResult(
                tool_name=invocation.tool_name,
                success=False,
                error_message=f"EmoteToolProvider 不支持的工具: {invocation.tool_name}",
            )

        service = self._service()
        if service is None or not state.is_connected():
            return ToolExecutionResult(
                tool_name=invocation.tool_name,
                success=False,
                error_message="表情功能未就绪：VTuber 桥未启用或 VTube Studio 未连接",
            )

        emotion = str(invocation.arguments.get("emotion") or "").strip()
        if not emotion:
            return ToolExecutionResult(
                tool_name=invocation.tool_name,
                success=False,
                error_message="缺少 emotion 参数",
            )

        try:
            message = await service.activate(emotion)
        except ValueError as exc:
            return ToolExecutionResult(
                tool_name=invocation.tool_name,
                success=False,
                error_message=str(exc),
            )
        except Exception as exc:  # noqa: BLE001 - VTS 失败要回给模型而不是中断本轮
            return ToolExecutionResult(
                tool_name=invocation.tool_name,
                success=False,
                error_message=f"表情注入失败：{exc!r}",
            )
        return ToolExecutionResult(
            tool_name=invocation.tool_name,
            success=True,
            content=message,
        )

    async def close(self) -> None:
        """释放 Provider 资源；服务由全局单例持有，这里不做处理。"""
