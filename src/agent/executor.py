"""Agent 工具执行器：把工具名/参数分发给注册工具，产出观察文本。

工具注册表 = {name: (schema, callable)}，callable 为同步/异步均可。
执行前先过沙箱门禁（高风险未放行直接返回拒绝文本，不抛异常——
观察结果要让 LLM 能读到原因并调整策略）。
"""

from __future__ import annotations

import asyncio
import inspect
import json
from typing import Any, Awaitable, Callable

from src.agent.sandbox import Sandbox, SandboxViolation

# 工具定义：(OpenAI function schema, 执行函数)
ToolEntry = tuple[dict, Callable[..., Any]]

# LLM 常见幻觉参数别名 → schema 规范参数名（仅当目标参数在 schema 中合法时生效）
_PARAM_ALIASES = {
    "filepath": "path",
    "file_name": "path",
    "filename": "path",
    "file": "path",
    "text": "content",
    "cmd": "command",
}


def _normalize_args(args: Any, schema: dict) -> dict:
    """把 LLM 可能幻觉的参数规整为 schema 声明的参数。

    - args 为 JSON 字符串时先解析；
    - 非 schema 声明的键丢弃（防幻觉参数触发 TypeError）；
    - 别名键映射到规范参数名，且不覆盖已提供的规范参数。
    """
    if isinstance(args, str):
        args = json.loads(args or "{}")
    if not isinstance(args, dict):
        return {}
    properties = schema.get("parameters", {}).get("properties", {})
    if not properties:
        return args
    normalized = {}
    for key, value in args.items():
        if key in properties:
            normalized[key] = value
        else:
            target = _PARAM_ALIASES.get(key)
            if target and target in properties and target not in normalized:
                normalized[target] = value
    return normalized


class ToolExecutor:
    def __init__(self, tools: dict[str, ToolEntry], sandbox: Sandbox) -> None:
        self._tools = tools
        self._sandbox = sandbox

    @property
    def schemas(self) -> list[dict]:
        """OpenAI function schema 列表（供 LLM tools 参数使用）。"""
        return [entry[0] for entry in self._tools.values()]

    def register(self, name: str, schema: dict, fn: Callable[..., Any]) -> None:
        """运行时注册工具（如 ReActAgent 注册 delegate 委派工具）。"""
        self._tools[name] = (schema, fn)

    def without(self, *names: str) -> "ToolExecutor":
        """返回去掉指定工具的新执行器（共享沙箱），供子 Agent 委派去递归用。"""
        return ToolExecutor(
            {k: v for k, v in self._tools.items() if k not in names},
            self._sandbox,
        )

    async def execute(self, name: str, args: dict) -> str:
        """执行工具并返回观察文本；任何失败都以文本形式返回（不抛异常）。"""
        entry = self._tools.get(name)
        if entry is None:
            return f"未知工具：{name}（可用：{', '.join(self._tools)}）"
        if not self._sandbox.check(name):
            return f"操作被沙箱拒绝（高风险未放行）：{name}"
        schema, fn = entry
        try:
            params = _normalize_args(args, schema)
        except (ValueError, json.JSONDecodeError) as e:
            props = schema.get("parameters", {}).get("properties", {})
            return f"参数错误：无法解析参数 JSON（{e}）；可用参数：{', '.join(props)}"
        try:
            result = fn(self._sandbox, **params)
            if inspect.isawaitable(result):
                result = await result
        except SandboxViolation as e:
            return f"沙箱拦截：{e}"
        except TypeError as e:
            # 参数不匹配：提示可用参数，让 LLM 修正
            props = schema.get("parameters", {}).get("properties", {})
            return f"参数错误：{e}；可用参数：{', '.join(props)}"
        except Exception as e:
            return f"执行失败：{type(e).__name__}: {e}"
        return str(result)[:4000]
