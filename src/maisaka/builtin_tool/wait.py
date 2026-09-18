"""wait 内置工具。"""

from typing import Optional

from src.core.tooling import ToolExecutionContext, ToolExecutionResult, ToolInvocation, ToolSpec

from .context import BuiltinToolRuntimeContext


def get_tool_spec() -> ToolSpec:
    """获取 wait 工具声明。"""

    return ToolSpec(
        name="wait",
        description="暂停当前对话并固定等待一段时间。",
        parameters_schema={
            "type": "object",
            "properties": {
                "seconds": {
                    "type": "integer",
                    "description": "等待秒数。",
                },
            },
            "required": ["seconds"],
        },
        provider_name="maisaka_builtin",
        provider_type="builtin",
    )


async def handle_tool(
    tool_ctx: BuiltinToolRuntimeContext,
    invocation: ToolInvocation,
    context: Optional[ToolExecutionContext] = None,
) -> ToolExecutionResult:
    """执行 wait 内置工具。"""

    del context, invocation
    # 低延迟优化：wait 由"挂起等待"改为"跳过"——
    # 不再进入等待状态、不占用等待时长，本轮直接结束。
    # 这样即使 planner 仍选择 wait，也不会造成无意义的挂起等待。
    return tool_ctx.build_success_result(
        "wait",
        "已跳过等待（wait 已停用为跳过语义），本轮不挂起、不额外等待。",
        metadata={
            "pause_execution": True,
            "skipped_wait": True,
        },
    )
