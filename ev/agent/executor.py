"""Agent 工具执行器：把工具名/参数分发给注册工具，产出观察文本。

工具注册表 = {name: (schema, callable)}，callable 为同步/异步均可。
执行前先过沙箱门禁（高风险未放行直接返回拒绝文本，不抛异常——
观察结果要让 LLM 能读到原因并调整策略）。
"""

from __future__ import annotations

import asyncio
import inspect
import json
import re
from typing import Any, Awaitable, Callable

from ev.agent.sandbox import Sandbox, SandboxViolation
from ev.kernel import estop

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


# 尾逗号清洗：{"a":1,} → {"a":1}（LLM 偶发生成）
_TRAILING_COMMA_RE = re.compile(r',\s*([}\]])')


def _repair_json(text: str) -> str:
    """清洗 LLM 偶发 JSON 格式错误（纯正则，不引依赖）。

    - 尾逗号：{"a":1,} → {"a":1}
    - 控制字符剔除（\\x00-\\x1f → 空格，防 json.loads 报控制字符错）
    不碰单引号（内容里的单引号会被误伤，风险高于收益）。
    """
    if not text:
        return text
    cleaned = _TRAILING_COMMA_RE.sub(r'\1', text)
    cleaned = re.sub(r'[\x00-\x1f]', ' ', cleaned)
    return cleaned


def _normalize_with_repair(args: Any, schema: dict) -> dict | str:
    """normalize args；JSON 字符串解析失败时清洗后重试一次。

    返回 dict（成功）或 str 错误文本（失败，含可用参数提示）。
    本地重试只覆盖 JSON 格式错误（尾逗号/控制字符）；参数语义错误
    （必填缺失/类型不符）由 loop ReAct 循环承担反馈——observation 是
    错误文本，下一步 _plan 让模型重新生成参数。
    """
    try:
        return _normalize_args(args, schema)
    except (ValueError, json.JSONDecodeError) as e:
        if isinstance(args, str):
            try:
                return _normalize_args(_repair_json(args), schema)
            except (ValueError, json.JSONDecodeError) as e2:
                props = schema.get("parameters", {}).get("properties", {})
                return f"参数错误：无法解析参数 JSON（{e2}）；可用参数：{', '.join(props)}"
        props = schema.get("parameters", {}).get("properties", {})
        return f"参数错误：无法解析参数（{e}）；可用参数：{', '.join(props)}"


def _validate_args(args: dict, schema: dict) -> tuple[bool, str]:
    """schema 校验：必填字段缺失检查（纯字典，不引 pydantic）。

    返回 (ok, error)：ok=True 时 error 为空。仅查必填缺失——
    类型错误由后续执行 TypeError 兜底（含可用参数提示），避免重复校验。
    """
    params = schema.get("parameters", {})
    required = params.get("required", [])
    missing = [r for r in required if r not in args]
    if missing:
        props = params.get("properties", {})
        return False, f"缺少必填参数：{missing}；可用参数：{', '.join(props)}"
    return True, ""


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
        """执行工具并返回观察文本；任何失败都以文本形式返回（不抛异常）。

        容错：JSON 清洗重试（尾逗号/控制字符）+ schema 校验前置（必填缺失
        提前拦截）+ 错误信息增强（含 required/可用参数）。参数语义错误
        靠 loop ReAct 循环反馈——observation 是错误文本，下一步 _plan 让
        模型重新生成参数。
        """
        entry = self._tools.get(name)
        if entry is None:
            return f"未知工具：{name}（可用：{', '.join(self._tools)}）"
        # 3.13 全局急停：哨兵文件存在时拒绝高危工具执行（fail-closed）
        if estop.is_blocked(name):
            return f"操作被全局急停拒绝（哨兵文件存在）：{name}"
        if not self._sandbox.check(name):
            return f"操作被沙箱拒绝（高风险未放行）：{name}"
        schema, fn = entry
        # normalize + JSON 清洗重试（失败返回错误文本，含可用参数）
        params = _normalize_with_repair(args, schema)
        if isinstance(params, str):
            return params
        # schema 校验前置：必填字段缺失提前拦截（含 required 列表）
        ok, err = _validate_args(params, schema)
        if not ok:
            return f"参数错误：{err}"
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
