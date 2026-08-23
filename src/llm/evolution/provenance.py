"""技能写入来源标记（对标 hermes tools/skill_provenance.py）。

后台复盘/策展与用户前台写入通过 ContextVar 区分来源：

- `_write_origin`：默认 ``"foreground"``（用户/主播前台写入）；
- ``BACKGROUND_REVIEW`` 哨兵：复盘、Agent 任务沉淀等后台路径置为
  ``"background_review"``；
- 技能创建（evolution.skills.save_skill）据此标记 `created_by`：
  background → agent（进入 curator 管理范围），foreground → user。

红线（用户资产边界）：**用户前台写入的技能永远不自动策展**——curator 只
整合/修剪后台自己创建的技能，避免后台自动改动主播手工维护的资产。
"""

from contextlib import contextmanager
from contextvars import ContextVar

_write_origin: ContextVar[str] = ContextVar(
    "skill_write_origin", default="foreground")

# 后台复盘/策展上下文哨兵（对标 hermes BACKGROUND_REVIEW）
BACKGROUND_REVIEW = "background_review"

# 前台（用户/主播）上下文
FOREGROUND = "foreground"


@contextmanager
def background_review_context():
    """把当前任务标记为后台复盘/策展上下文（技能写入按 agent 溯源，5.5）。

    ContextVar 在 asyncio 任务内传播，复盘/审阅整条落地链（save_skill、
    apply_patch、maybe_prune 等）与 Agent 任务沉淀中的技能创建都会读到
    background 来源。用法：``with background_review_context(): ...``
    """
    set_write_origin(BACKGROUND_REVIEW)
    try:
        yield
    finally:
        reset_write_origin()


def set_write_origin(origin: str) -> None:
    """设置当前执行上下文（任务内）的写入来源：foreground / background_review。"""
    _write_origin.set(origin)


def reset_write_origin() -> None:
    """复位为前台来源（默认值）。"""
    _write_origin.set(FOREGROUND)


def get_current_write_origin() -> str:
    """返回当前写入来源。"""
    return _write_origin.get()


def is_background_review() -> bool:
    """当前是否处于后台复盘/策展上下文（决定 created_by 标记）。"""
    return _write_origin.get() == BACKGROUND_REVIEW
