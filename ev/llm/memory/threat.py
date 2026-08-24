"""记忆写入威胁扫描（对标 hermes threat_patterns.py 的 strict 范围）。

记忆是持久化 Prompt 入口：写入 MEMORY.md / USER.md 的内容会整会话注入
system prompt（冻结快照），被污染后影响面大且难清除。因此写入前必须扫描，
命中即拒绝。只依赖标准库正则，不引入第三方依赖。四类威胁：

- Prompt 注入：要求模型忽略指令 / 覆盖系统设定；
- 角色劫持：要求模型冒充系统 / 管理员；
- 密钥提取：诱导输出或直接落盘 API Key / 密码 / token / 私钥；
- 隐形 Unicode：零宽字符 / 双向文本控制符（肉眼不可见地注入指令）。

扫描只做「命中即拒绝」，不做内容改写——改写的安全边界不清晰，且会破坏
用户原始文本的可审计性。
"""

from __future__ import annotations

import re
from typing import Optional

# (正则, 威胁名)：命中即拒绝该条记忆写入
_THREAT_PATTERNS = [
    # ---- Prompt 注入：指令忽略 / 系统设定覆盖 ----
    (re.compile(
        r"(忽略|无视|忘掉|覆盖)\s*(以上|之前|前面|下列)?\s*(所有)?\s*"
        r"(指令|提示|规则|系统设定|设定)", re.IGNORECASE),
     "prompt injection（要求忽略指令）"),
    (re.compile(
        r"(ignore|disregard|forget|override|unfollow)\s+(all|any|the|"
        r"previous|above|below)?\s*(instructions?|prompts?|rules?|"
        r"system|guidelines?)", re.IGNORECASE),
     "prompt injection（ignore/override）"),
    # ---- 角色劫持：冒充系统 / 管理员 ----
    (re.compile(r"(你现在|你就是|你要(变成|扮演))\s*(系统|管理员|上帝|后台)", re.IGNORECASE),
     "role hijack（冒充系统/管理员）"),
    (re.compile(
        r"(pretend|act)\s+(to\s+be|as)\s+(a\s+)?(system|admin|root|assistant)",
        re.IGNORECASE),
     "role hijack（pretend/act as）"),
    # ---- 密钥提取 / 敏感信息泄露 ----
    (re.compile(
        r"(输出|打印|泄露|给出|告诉我)\s*(你的|所有)?\s*(api\s*key|apikey|"
        r"密钥|密码|口令|token|secret|access\s*token|私钥|验证码)",
        re.IGNORECASE),
     "secret extraction（诱导输出密钥）"),
    (re.compile(
        r"(api[_\s-]?key|access[_\s-]?token|secret|pass(word|wd)?|"
        r"private[_\s-]?key|authorization)\s*[=:：]\s*\S",
        re.IGNORECASE),
     "secret leak（疑似密钥明文）"),
]

# 隐形 Unicode：零宽字符 + 双向文本控制符 + 私用区标签符
_INVISIBLE_UNICODE_RE = re.compile(
    "[\u200b-\u200f\u202a-\u202e\u2060-\u206f\ufeff\ufff9-\ufffb\ufdd0-\ufdef]")


def first_threat_message(content: str) -> Optional[str]:
    """扫描记忆内容，命中威胁返回错误说明，否则返回 None。"""
    if not content:
        return None
    if _INVISIBLE_UNICODE_RE.search(content):
        return ("内容包含不可见 Unicode 字符（零宽/双向控制符），已拒绝写入。"
                "请去除这些隐形字符后重试。")
    for pattern, name in _THREAT_PATTERNS:
        if pattern.search(content):
            return (f"内容疑似{name}，已拒绝写入"
                    "（记忆是持久化 Prompt 入口，不能包含攻击载荷）。")
    return None
