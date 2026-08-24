"""ProfanityFilter 初始化（L346-350）。"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ev.kernel.runtime import RuntimeContext


async def setup(runtime: "RuntimeContext") -> None:
    """ProfanityFilter 初始化（弹幕回复 / 主动对话 / 用户对话共用）。"""
    from ev.utils import console
    from ev.utils.content_filter import ProfanityFilter

    cfg = runtime.cfg
    # 内容过滤（弹幕回复 / 主动对话 / 用户对话共用，需在引擎创建前就绪）
    runtime.pf = ProfanityFilter() if cfg.PROFANITY_FILTER_ENABLED else None
    if runtime.pf is not None:
        console.dim(f"内容过滤已启用：检测到骂人用语时 "
                    f"{cfg.PROFANITY_FILTER_RATE:.0%} 概率触发（替换为 Filter）")


async def teardown(runtime: "RuntimeContext") -> None:
    """ProfanityFilter 无显式清理。"""
    return None
