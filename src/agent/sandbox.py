"""Agent 沙箱：路径越界校验 + 高风险操作门禁。

- 所有文件类工具必须落在工作空间（AGENT_WORKSPACE，默认项目根）内，
  归一化后做前缀校验，越界抛 SandboxViolation。
- run_shell 等高风险操作默认拒绝；仅 AGENT_ALLOW_SHELL=true 显式放行。
"""

from __future__ import annotations

import re
from pathlib import Path

# 高风险操作：默认拒绝（需 AGENT_ALLOW_SHELL=true 或人工审批放行）
HIGH_RISK_TOOLS = {"run_shell", "delete_file", "delete_directory"}


class SandboxViolation(Exception):
    """沙箱拒绝执行（路径越界 / 高风险未放行）。"""


class Sandbox:
    def __init__(self, *, root: str, allow_shell: bool = False) -> None:
        self.root = Path(root).resolve()
        self.allow_shell = allow_shell

    def check(self, tool_name: str) -> bool:
        """操作级门禁：True = 允许。高风险工具未显式放行一律拒绝。"""
        if tool_name in HIGH_RISK_TOOLS and not self.allow_shell:
            return False
        return True

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
