"""Agent Token 预算：限制单次任务的 LLM 消耗，防止失控循环。

- 上限来源：显式 max_tokens（如 .env AGENT_MAX_TOKENS）；未提供时按
  model_name 映射模型上下文窗口（对标 Firefly TokenBudget）。
- 触发线：默认达到窗口 75% 即触发历史压缩，而非等到 100% 满载才处理。
- 估算：中英混合文本约 2.5 字符/token（_CHARS_PER_TOKEN）；LLM usage
  返回的精确 token 数通过 consume() 累加。
"""

from __future__ import annotations

from fnmatch import fnmatchcase
from typing import Optional


# 常见模型上下文窗口（token）；键支持 fnmatch 通配符（* / ?），
# 按字典顺序首次命中即生效，未命中的模型回退 _DEFAULT_LIMIT
_MODEL_LIMITS: dict[str, int] = {
    # ── DeepSeek（旗舰百万级窗口，其余 64K） ──
    "deepseek-v4-pro": 1_000_000,
    "deepseek-v4-flash": 1_000_000,
    "deepseek-*": 64_000,
    # ── 通义千问（旗舰百万级窗口，其余 32K） ──
    "qwen3-max": 1_000_000,
    "qwen3-plus": 1_000_000,
    "qwen3-flash": 1_000_000,
    "qwen-*": 32_000,
    # ── 智谱 GLM 全系 128K 标准窗口 ──
    "glm-*": 128_000,
    # ── OpenAI ──
    "gpt-4o": 128_000,
    "gpt-4": 128_000,
    "gpt-3.5-turbo": 16_384,
}

_DEFAULT_LIMIT = 128_000


def model_to_max_context(model_name: str) -> int:
    """按模型名映射上下文窗口大小（token）；未知模型回退默认窗口。"""
    if not model_name:
        return _DEFAULT_LIMIT
    for pattern, limit in _MODEL_LIMITS.items():
        if fnmatchcase(model_name, pattern):
            return limit
    return _DEFAULT_LIMIT


class TokenBudget:
    """轻量 Token 预算跟踪器：上限可显式指定或按模型窗口映射。"""

    _CHARS_PER_TOKEN = 2.5  # 中英混合保守估计

    def __init__(
        self,
        max_tokens: Optional[int] = None,
        *,
        model_name: Optional[str] = None,
        trigger_ratio: float = 0.75,
    ) -> None:
        self.model_name = model_name
        if max_tokens and max_tokens > 0:
            self.max_tokens = max_tokens
        else:
            self.max_tokens = model_to_max_context(model_name)
        self.trigger_threshold = max(1, int(self.max_tokens * trigger_ratio))
        self._used = 0.0

    def consume(self, tokens: int) -> None:
        """累加 LLM usage 报告的精确 token 数（负数忽略）。"""
        self._used += max(int(tokens), 0)

    def add(self, text: str) -> None:
        """按字符数估算累加文本 token 用量。"""
        if text:
            self._used += len(text) / self._CHARS_PER_TOKEN

    def reset(self) -> None:
        self._used = 0.0

    @property
    def used(self) -> int:
        return int(self._used)

    @property
    def remaining(self) -> int:
        return max(0, self.max_tokens - int(self._used))

    def is_full(self, estimate: int = 0) -> bool:
        """已用（+预估）达到触发线即视为满载（触发历史压缩）。"""
        return int(self._used) + estimate >= self.trigger_threshold
