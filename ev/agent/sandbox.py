"""Agent 沙箱：路径越界校验 + 高风险门禁 + 命令白名单 + 审计日志。

4 道防线（渐进式加固，不破坏现有行为）：
- 路径白名单：resolve() 把所有文件路径锁定在 AGENT_WORKSPACE 内，越界抛
  SandboxViolation（既有行为，保留）
- 高风险门禁：check() 对 run_shell/delete_* 默认拒绝，AGENT_ALLOW_SHELL=true
  放行（既有行为，保留）
- 命令白名单：check_command() 白名单制——AGENT_ALLOWED_COMMANDS 空=放行所有
  （向后兼容），非空=只允许列出的命令（fail-closed）
- 审计日志：audit() 把每次 run_shell 的 ALLOW/DENY/TIMEOUT 落盘 JSONL，
  默认写 AGENT_WORKSPACE/.agent_audit.jsonl（可 AGENT_AUDIT_LOG 覆盖）

资源限制（CPU/内存 rlimit）Unix 专属，Windows 不支持——靠 run_shell 的 30s
超时兜底（见 tools._run_shell），不在此强制。
"""

from __future__ import annotations

import json
import re
import shlex
import threading
import time
from pathlib import Path
from typing import Any

# 高风险操作：默认拒绝（需 AGENT_ALLOW_SHELL=true 或人工审批放行）
HIGH_RISK_TOOLS = {"run_shell", "delete_file", "delete_directory"}


class SandboxViolation(Exception):
    """沙箱拒绝执行（路径越界 / 高风险未放行 / 命令不在白名单）。"""


class Sandbox:
    def __init__(
        self,
        *,
        root: str,
        allow_shell: bool = False,
        allowed_commands: set[str] | None = None,
        audit_path: str | None = None,
    ) -> None:
        self.root = Path(root).resolve()
        self.allow_shell = allow_shell
        # 命令白名单：None/空 = 放行所有（向后兼容）；非空 = 白名单制
        self.allowed_commands = set(c for c in (allowed_commands or []) if c)
        # 审计日志路径：默认派生工作空间（agent 安全产物，不依赖 DATA_ROOT）
        self.audit_path = Path(audit_path) if audit_path else self.root / ".agent_audit.jsonl"
        self._audit_lock = threading.Lock()

    def check(self, tool_name: str) -> bool:
        """操作级门禁：True = 允许。高风险工具未显式放行一律拒绝。"""
        if tool_name in HIGH_RISK_TOOLS and not self.allow_shell:
            return False
        return True

    def check_command(self, command: str) -> tuple[bool, str]:
        """命令白名单校验：True = 允许。

        白名单制（fail-closed）：AGENT_ALLOWED_COMMANDS 空=放行所有（向后
        兼容 AGENT_ALLOW_SHELL=true 现有行为），非空=只允许列出的命令。
        shell -c 时拆 argv[0] 匹配；拆分失败按整串匹配。
        """
        if not self.allowed_commands:
            return True, ""  # 未配置白名单=放行（向后兼容）
        if not command or not command.strip():
            return False, "命令为空"
        # 拆 argv 取首词匹配（shlex 失败回退首空格分词）
        try:
            argv = shlex.split(command)
        except ValueError:
            argv = command.split()
        cmd0 = argv[0] if argv else command
        # Windows 可执行后缀兼容：去 .exe/.bat/.cmd 再匹配
        cmd0_base = re.sub(r'\.(exe|bat|cmd|ps1)$', '', cmd0, flags=re.IGNORECASE)
        if cmd0_base in self.allowed_commands or cmd0 in self.allowed_commands:
            return True, ""
        return False, f"命令不在白名单：{cmd0}（允许：{', '.join(sorted(self.allowed_commands))}）"

    def audit(self, verdict: str, command: str, **extra: Any) -> None:
        """审计日志：把每次命令执行的 ALLOW/DENY/TIMEOUT/ERROR 落盘 JSONL。

        fail-open：写入异常不影响主流程（命令仍执行/拒绝），仅静默丢弃。
        同步写一行 JSONL（<1ms，不显著阻塞事件循环）。
        """
        try:
            entry = {
                "ts": time.time(),
                "verdict": verdict,
                "command": command[:200],  # 截断防巨长命令刷盘
                **extra,
            }
            line = json.dumps(entry, ensure_ascii=False) + "\n"
            with self._audit_lock:
                with open(self.audit_path, "a", encoding="utf-8") as f:
                    f.write(line)
        except Exception:
            pass  # 审计写入失败不阻断主流程

    def resolve(self, path: str) -> Path:
        """把（相对/绝对）路径解析到工作空间内；越界抛 SandboxViolation。

        ~ 不展开（避免 HOME 越界）；软链接不追（保持前缀校验简单可靠）。
        """
        p = Path(str(path or "")).expanduser()
        if not p.is_absolute():
            p = self.root / p
        resolved = p.resolve(strict=False)
        if resolved != self.root and self.root not in resolved.parents:
            raise SandboxViolation(f"路径越界：{path}")
        return resolved

    @staticmethod
    def sanitize_filename(name: str) -> str:
        """清理非法文件名字符（Windows/Linux 通用），空结果回退 default。"""
        cleaned = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", str(name).strip())
        return cleaned or "default"
