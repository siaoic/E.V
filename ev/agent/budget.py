"""Agent Token 预算：限制单次任务的 LLM 消耗，防止失控循环。

- 上限来源：显式 max_tokens（如 .env AGENT_MAX_TOKENS）；未提供时按
  model_name 映射模型上下文窗口（对标 Firefly TokenBudget）。
- 触发线：默认达到窗口 75% 即触发历史压缩，而非等到 100% 满载才处理。
- 估算：中英混合文本约 2.5 字符/token（_CHARS_PER_TOKEN）；LLM usage
  返回的精确 token 数通过 consume() 累加。
"""

from __future__ import annotations

import threading
import time
from collections import deque
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


# ──────────────────────────────────────────────────────────────────────────
# 全局时间窗口预算（与 TokenBudget 正交：单次任务级 vs 全局时间窗口级）
# ──────────────────────────────────────────────────────────────────────────


class UnifiedBudget:
    """全局时间窗口预算：token/分钟 + 请求/分钟 + 成本/小时 三轴限流。

    与 TokenBudget（单次任务级，管一个 ReAct 任务的 token 上限）正交：
    UnifiedBudget 管全局时间窗口的总配额，防 burst 洪峰（如 auxiliary
    高频调用 + evolution 复盘 + butler 提取并发时跨配额）。

    默认 0=禁用对应轴（向后兼容，不限制）；配置非 0 即启用限流。
    滑动窗口用 deque + 时间戳，过期自动清理（GC 在 acquire 时触发）。
    """

    def __init__(
        self,
        *,
        token_per_minute: int = 0,       # 0=禁用
        request_per_minute: int = 0,     # 0=禁用
        cost_per_hour_usd: float = 0.0,   # 0=禁用
    ) -> None:
        self.token_per_minute = token_per_minute
        self.request_per_minute = request_per_minute
        self.cost_per_hour_usd = cost_per_hour_usd
        self._token_window: "deque[tuple[float, int]]" = deque()
        self._request_window: "deque[float]" = deque()
        self._cost_window: "deque[tuple[float, float]]" = deque()
        self._lock = threading.Lock()

    def acquire(self, estimated_tokens: int = 0,
                estimated_cost: float = 0.0) -> bool:
        """请求配额：三轴检查，超额返回 False（fail-closed 保守）。

        0=禁用的轴跳过检查；通过后累加到对应窗口。调用方在调 LLM 前调
        acquire，False 时应降级或排队（如用旧结果 / 延迟到下个窗口）。
        """
        now = time.time()
        with self._lock:
            self._gc(now)
            # token 轴
            if self.token_per_minute > 0:
                used = sum(t for _, t in self._token_window)
                if used + estimated_tokens > self.token_per_minute:
                    return False
            # request 轴
            if self.request_per_minute > 0:
                if len(self._request_window) >= self.request_per_minute:
                    return False
            # cost 轴
            if self.cost_per_hour_usd > 0:
                spent = sum(c for _, c in self._cost_window)
                if spent + estimated_cost > self.cost_per_hour_usd:
                    return False
            # 通过：累加到窗口
            if self.token_per_minute > 0 and estimated_tokens > 0:
                self._token_window.append((now, estimated_tokens))
            if self.request_per_minute > 0:
                self._request_window.append(now)
            if self.cost_per_hour_usd > 0 and estimated_cost > 0:
                self._cost_window.append((now, estimated_cost))
            return True

    def _gc(self, now: float) -> None:
        """清理过期窗口：token/请求 1 分钟，成本 1 小时。"""
        minute_ago = now - 60.0
        hour_ago = now - 3600.0
        while self._token_window and self._token_window[0][0] < minute_ago:
            self._token_window.popleft()
        while self._request_window and self._request_window[0] < minute_ago:
            self._request_window.popleft()
        while self._cost_window and self._cost_window[0][0] < hour_ago:
            self._cost_window.popleft()

    def stats(self) -> dict:
        """当前窗口用量（可观测，控制中心 UI 可读）。"""
        now = time.time()
        with self._lock:
            self._gc(now)
            return {
                "token_used_min": sum(t for _, t in self._token_window),
                "requests_min": len(self._request_window),
                "cost_used_hour": sum(c for _, c in self._cost_window),
            }


# 进程内单例（懒加载，默认全禁用）
_unified_budget: Optional[UnifiedBudget] = None


def get_unified_budget() -> UnifiedBudget:
    """获取全局预算单例（懒加载，默认全轴禁用=不限制）。

    调用方需启用限流时，通过 configure_unified_budget(cfg) 注入阈值。
    """
    global _unified_budget
    if _unified_budget is None:
        _unified_budget = UnifiedBudget()
    return _unified_budget


def configure_unified_budget(
    *,
    token_per_minute: int = 0,
    request_per_minute: int = 0,
    cost_per_hour_usd: float = 0.0,
) -> UnifiedBudget:
    """配置全局预算阈值（替换单例）；0=禁用对应轴。

    通常在 Application 启动时从 cfg 读 BUDGET_* 注入。默认全 0=不限制，
    不破坏现有行为；配置非 0 即启用对应轴限流。
    """
    global _unified_budget
    _unified_budget = UnifiedBudget(
        token_per_minute=token_per_minute,
        request_per_minute=request_per_minute,
        cost_per_hour_usd=cost_per_hour_usd,
    )
    return _unified_budget
