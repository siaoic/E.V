"""MCPManager 创建 + initialize + tools 合并 + skill manager 就绪打印（L254-274）。"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ev.kernel.runtime import RuntimeContext


async def setup(runtime: "RuntimeContext") -> None:
    """MCPManager 初始化 + get_merged_tools + skill manager 就绪打印。"""
    from ev.utils import console
    from ev.mcp.manager import MCPManager

    cfg = runtime.cfg

    # MCP 管理器
    runtime.mcp = MCPManager() if (cfg.MCP_ENABLED and cfg.TOOLS_ENABLED) else None
    if runtime.mcp is not None:
        await runtime.mcp.initialize()
        runtime.mcp.warmup()
    from plugins.builtin.tools import get_merged_tools
    merged_tools = get_merged_tools(runtime.mcp)
    if merged_tools:
        names = [t["function"]["name"] for t in merged_tools]
        console.dim(f"工具已就绪（{len(names)} 个）：{'、'.join(names)}")
    else:
        console.dim("无可用工具：AI 将以纯对话模式运行（MCP 未启用且未配置搜索/天气 key）")

    # 技能系统
    from plugins.builtin.tools.skills import get_skill_manager
    skill_mgr = get_skill_manager()
    if skill_mgr.skills:
        names = [s.name for s in skill_mgr.skills]
        console.dim(f"技能已就绪（{len(names)} 个）：{'、'.join(names)}"
                    f"（load_skill 按需加载，改文件无需重启）")
    else:
        console.dim("技能目录为空：未发现技能（SKILLS_DIR/<技能名>/SKILL.md）")


async def teardown(runtime: "RuntimeContext") -> None:
    """原 teardown 中 MCP stop 段。"""
    import asyncio

    if runtime.mcp is not None:
        try:
            await asyncio.wait_for(runtime.mcp.stop(), timeout=15)
        except Exception:
            pass
