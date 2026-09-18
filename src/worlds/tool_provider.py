"""世界工具 Provider：把 World 框架的通用工具暴露给 Planner。

目前只有一个工具 ``world_observe``——它是「按需拉取」的出口：AI 想知道世界
此刻的细节时直接拉一份即时状态，不必干等事件或下次请求。

工具只在「当前会话就是被绑定的直播聊天流」时才暴露，避免无关会话白拿一份
无效工具定义。
"""

from __future__ import annotations

from typing import Optional

from src.core.tooling import (
    ToolAvailabilityContext,
    ToolExecutionContext,
    ToolExecutionResult,
    ToolInvocation,
    ToolSpec,
)

from .manager import WorldManager, get_world_manager

OBSERVE_TOOL_NAME = "world_observe"

OBSERVE_TOOL_DESCRIPTION = (
    "拉取某个世界的即时完整状态（绕过缓存），用于需要确认当前画面/进度细节时。"
    "不传 world_name 时列出当前已接入的全部世界。"
)


class WorldToolProvider:
    """把世界框架的通用工具注册进统一工具注册表。"""

    provider_name = "worlds"
    provider_type = "world"

    def __init__(self, manager: Optional[WorldManager] = None) -> None:
        """初始化 Provider。

        Args:
            manager: 世界管理器；留空时按需取全局单例。
        """

        self._manager = manager

    @property
    def manager(self) -> WorldManager:
        """当前使用的世界管理器。"""

        if self._manager is None:
            self._manager = get_world_manager()
        return self._manager

    async def list_tools(
        self,
        context: Optional[ToolAvailabilityContext] = None,
    ) -> list[ToolSpec]:
        """列出世界工具；当前会话不是绑定的直播聊天流时返回空列表。"""

        manager = self.manager
        session_id = ""
        if context is not None:
            session_id = str(context.stream_id or context.session_id or "").strip()
        if not session_id or session_id != manager.bound_session_id:
            return []
        if not manager.registry.descriptors():
            return []

        return [
            ToolSpec(
                name=OBSERVE_TOOL_NAME,
                description=OBSERVE_TOOL_DESCRIPTION,
                title="观察世界",
                parameters_schema={
                    "type": "object",
                    "properties": {
                        "world_name": {
                            "type": "string",
                            "description": "世界机器名（如 minecraft / pvz / bilibili）；留空则列出全部已接入世界",
                        }
                    },
                    "required": [],
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
        """执行世界工具调用。"""

        if invocation.tool_name != OBSERVE_TOOL_NAME:
            return ToolExecutionResult(
                tool_name=invocation.tool_name,
                success=False,
                error_message=f"WorldToolProvider 不支持的工具: {invocation.tool_name}",
            )

        manager = self.manager
        world_name = str(invocation.arguments.get("world_name") or "").strip()

        if not world_name:
            descriptors = manager.registry.descriptors()
            if not descriptors:
                return ToolExecutionResult(
                    tool_name=invocation.tool_name,
                    success=True,
                    content="当前没有接入任何世界。",
                )
            lines = ["当前已接入的世界："]
            lines.extend(
                f"- {descriptor.display_name}（world_name={descriptor.name}）" for descriptor in descriptors
            )
            return ToolExecutionResult(
                tool_name=invocation.tool_name,
                success=True,
                content="\n".join(lines),
            )

        # 调用失败要把原因回给模型，而不是抛出异常中断本轮
        try:
            snapshot = await manager.observe(world_name)
        except (KeyError, RuntimeError) as exc:
            return ToolExecutionResult(
                tool_name=invocation.tool_name,
                success=False,
                error_message=str(exc),
            )

        if not snapshot:
            return ToolExecutionResult(
                tool_name=invocation.tool_name,
                success=True,
                content=f"{world_name} 当前没有可读的状态。",
            )
        return ToolExecutionResult(
            tool_name=invocation.tool_name,
            success=True,
            content=snapshot,
        )

    async def close(self) -> None:
        """释放 Provider 资源；世界框架由全局单例持有，这里不做处理。"""
