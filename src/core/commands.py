"""命令注册表：把 /memory / !config / !tools / !model 等命令的匹配与派发解耦。

设计动机：原 _dispatch 是 100+ 行 if/elif 链，每加一条命令都要改主流程。
改为注册表后，新增命令 = 加一行 Command(...)，主循环不动。

字段：
  - prefix: 匹配前缀（如 "!model " 后面带空格；exact=True 时整行匹配）
  - handler: async 回调，签名 (app, cmd) -> bool，True 表示已消费
  - exact: True 表示 prefix 须整行匹配（用于 !config 这种独立命令）
  - exclusive: 是否独占（True 时命中后不再尝试其它匹配；本版本未启用，
               保留扩展位）
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable, List, Optional

# handler 签名：app 是 Application 实例，cmd 是用户输入整行；返回 True 表示已消费
CommandHandler = Callable[["object", str], Awaitable[bool]]


@dataclass(frozen=True)
class Command:
    prefix: str
    handler: CommandHandler
    exact: bool = False
    exclusive: bool = False
    help: str = ""


class CommandRegistry:
    """命令注册表：按注册顺序匹配 prefix（exact=True 整行匹配）。"""

    def __init__(self) -> None:
        self._cmds: List[Command] = []

    def register(self, *cmds: Command) -> None:
        """注册若干命令。重复 prefix 不去重，由 dispatch 顺序决定命中。"""
        self._cmds.extend(cmds)

    async def dispatch(self, app: "object", cmd: str) -> Optional[bool]:
        """按注册顺序尝试匹配：返回 handler 结果 / 命中后没 handler 返 True / 全不匹配返 None。"""
        for c in self._cmds:
            if c.exact:
                if cmd == c.prefix:
                    return await c.handler(app, cmd)
                continue
            if cmd.startswith(c.prefix):
                return await c.handler(app, cmd)
        return None

    def help_text(self) -> str:
        """返回所有注册命令的 help 文本（供 /help 等用）。"""
        lines: List[str] = []
        for c in self._cmds:
            if not c.help:
                continue
            tag = "（整行）" if c.exact else ""
            lines.append(f"  {c.prefix}{tag}  {c.help}")
        return "\n".join(lines)
