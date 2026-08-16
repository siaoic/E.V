"""Agent 内置工具注册：文件读写 / 目录浏览 / 受限 shell。

所有工具首参为 sandbox（注入），后续参数来自 LLM 的 function call。
文件路径统一经 sandbox.resolve 校验，越界由沙箱拦截。
"""

from __future__ import annotations

import asyncio
import os

from src.agent.executor import ToolEntry
from src.agent.sandbox import Sandbox

# 观察截断上限（按工具分档：文件读取大档、命令输出中档、目录列表小档）
_READ_LIMIT = 12000
_SHELL_LIMIT = 6000
_LIST_LIMIT = 2000


def _fold_observation(text: str, limit: int) -> str:
    """保头保尾折叠截断：超长输出保留首尾各半，中间折叠标注（防技术日志
    擦除 LLM 注意力，对标 Firefly _sanitize_observation）。"""
    text = (text or "").strip()
    if len(text) <= limit:
        return text or "（空）"
    half = (limit - 40) // 2
    head, tail = text[:half], text[-half:]
    folded = len(text) - len(head) - len(tail)
    return f"{head}\n…（中间 {folded} 字符已折叠）\n{tail}"


def _read_file(sandbox: Sandbox, path: str) -> str:
    p = sandbox.resolve(path)
    if not p.is_file():
        return f"文件不存在：{path}"
    try:
        return _fold_observation(p.read_text(encoding="utf-8", errors="replace"), _READ_LIMIT)
    except OSError as e:
        return f"读取失败：{e}"


def _write_file(sandbox: Sandbox, path: str, content: str) -> str:
    p = sandbox.resolve(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content or "", encoding="utf-8")
    return f"已写入 {p}（{len(content or '')} 字符）"


def _append_file(sandbox: Sandbox, path: str, content: str) -> str:
    p = sandbox.resolve(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(content or "")
    return f"已追加 {p}"


def _list_dir(sandbox: Sandbox, path: str = ".") -> str:
    p = sandbox.resolve(path)
    if not p.is_dir():
        return f"目录不存在：{path}"
    try:
        entries = sorted(os.listdir(p), key=str.lower)
    except OSError as e:
        return f"列目录失败：{e}"
    lines = [f"{d}/" if os.path.isdir(p / d) else d for d in entries]
    return _fold_observation("\n".join(lines), _LIST_LIMIT)


async def _run_shell(sandbox: Sandbox, command: str) -> str:
    """受限 shell：工作目录锁定在沙箱根；由 Sandbox.check 门禁（默认拒绝）。"""
    if not sandbox.allow_shell:
        return "shell 未启用（AGENT_ALLOW_SHELL=false），操作被沙箱拒绝"
    if not command or not command.strip():
        return "命令为空"
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=str(sandbox.root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
        return _fold_observation(out.decode("utf-8", errors="replace"), _SHELL_LIMIT)
    except asyncio.TimeoutError:
        proc.kill()
        return "命令执行超时（>30s），已终止"
    except OSError as e:
        return f"命令执行失败：{e}"


def build_builtin_tools() -> dict[str, ToolEntry]:
    """内置工具注册表：{name: (schema, fn)}。"""
    return {
        "read_file": ({
            "name": "read_file",
            "description": "读取工作空间内文本文件内容（UTF-8）。",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "文件路径（可相对工作空间）"}},
                "required": ["path"],
            },
        }, _read_file),
        "write_file": ({
            "name": "write_file",
            "description": "覆盖写入文本文件（自动创建父目录）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件路径"},
                    "content": {"type": "string", "description": "完整文件内容"},
                },
                "required": ["path", "content"],
            },
        }, _write_file),
        "append_file": ({
            "name": "append_file",
            "description": "追加内容到文本文件末尾。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件路径"},
                    "content": {"type": "string", "description": "要追加的内容"},
                },
                "required": ["path", "content"],
            },
        }, _append_file),
        "list_dir": ({
            "name": "list_dir",
            "description": "列出目录下的条目（目录带 / 后缀）。",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "目录路径，默认当前目录"}},
            },
        }, _list_dir),
        "run_shell": ({
            "name": "run_shell",
            "description": "在工作空间目录执行 shell 命令（高风险，默认被沙箱拒绝）。",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string", "description": "shell 命令"}},
                "required": ["command"],
            },
        }, _run_shell),
    }
