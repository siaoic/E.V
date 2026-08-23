"""统一工具注册表：注册 / 门控 / 分发（对标 Hermes tools/registry.py 精简落地）。

解决 3.2 差距：工具分散在 plugins/tools/defs.py（定义）与 _LOCAL_REGISTRY（实现）
中，无统一注册中心、无 check_fn 门控、handler 返回值格式不统一。

本模块提供：
- register(name, toolset, schema, handler, check_fn, override)：统一注册；
  跨 toolset 重名默认拒绝，override=True 显式许可；
- check_fn 门控三件套：30s TTL 缓存 / 60s last-good 宽限 / 512 上限 /
  fail-closed（check_fn 失败默认拒绝，但上次成功后 60s 内的瞬时失败按抖动放行）；
- dispatch_async()：统一分发，handler 返回值归一化为 JSON 字符串
  （dict/list 自动 json.dumps；str 原样返回；异常转 {"error": ...}）；
- bump_generation()：热重载后使 get_definitions 的 memo 失效。

设计约束（贴合本项目规范）：
- 增量层：TOOL_REGISTRY=0 时 executor/call_tool 走旧路径，行为 100% 不变；
- 未注册的工具 dispatch 返回 None，调用方回退旧路径（MCP/插件/本地兜底）；
- 不引入第三方库（缓存用标准库 dict + threading.Lock）。
"""

from __future__ import annotations

import inspect
import json
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from src.utils import console
from src.core import estop

# check_fn 门控参数（对标 Hermes tools/registry.py）
_GATE_CACHE_TTL = 30.0     # 门控结果缓存秒数：外部状态按分钟级变化，短时复用免探测
_GATE_GRACE = 60.0         # 上次成功后的宽限秒数：期内瞬时失败按抖动放行（last-good）
_GATE_CACHE_LIMIT = 512    # 门控缓存容量上限：超限淘汰最旧


@dataclass
class ToolEntry:
    """单个已注册工具的元信息。"""
    name: str
    toolset: str
    schema: dict
    handler: Callable[..., Any]        # 调用约定：handler(args: dict) -> Any
    check_fn: Optional[Callable[[], bool]] = None  # 可用性门控（无则恒可用）
    generation: int = 0                # 注册时的代际（热重载判定用）


class ToolRegistry:
    """进程内单例注册表：注册 / 门控 / 分发。"""

    def __init__(self) -> None:
        self._tools: Dict[str, ToolEntry] = {}
        # 门控缓存：name → (monotonic 时间戳, bool)；last-good：name → 最近成功时间
        self._gate_cache: Dict[str, tuple] = {}
        self._gate_last_good: Dict[str, float] = {}
        self._lock = threading.RLock()
        self._generation = 0

    # -- 注册 ----------------------------------------------------------------

    def register(
        self,
        name: str,
        toolset: str,
        schema: dict,
        handler: Callable[..., Any],
        check_fn: Optional[Callable[[], bool]] = None,
        override: bool = False,
    ) -> bool:
        """注册一个工具；跨 toolset 重名默认拒绝，override=True 显式许可。

        返回是否注册成功（重名被拒时返回 False 并告警）。
        """
        with self._lock:
            existing = self._tools.get(name)
            if existing is not None and existing.toolset != toolset:
                if not override:
                    console.warn(
                        f"[工具注册表] 拒绝注册 '{name}'：已存在 toolset "
                        f"'{existing.toolset}'，如需覆盖请传 override=True")
                    return False
                console.dim(
                    f"[工具注册表] '{name}' 覆盖原 toolset '{existing.toolset}'")
            self._tools[name] = ToolEntry(
                name=name,
                toolset=toolset,
                schema=schema,
                handler=handler,
                check_fn=check_fn,
                generation=self._generation,
            )
            self._generation += 1
            return True

    def deregister(self, name: str) -> None:
        """注销一个工具（插件卸载 / MCP 刷新时 nuke-and-repave）。"""
        with self._lock:
            self._tools.pop(name, None)
            self._gate_cache.pop(name, None)
            self._gate_last_good.pop(name, None)
            self._generation += 1

    def bump_generation(self) -> None:
        """热重载后调用：使外部对 get_definitions 的 memo 失效。"""
        with self._lock:
            self._generation += 1

    @property
    def generation(self) -> int:
        """当前代际：变更一次 +1，外部可据此缓存 get_definitions 结果。"""
        return self._generation

    # -- 查询 ----------------------------------------------------------------

    def get_entry(self, name: str) -> Optional[ToolEntry]:
        """按名取条目；未注册返回 None。"""
        with self._lock:
            return self._tools.get(name)

    def get_all_entries(self) -> List[ToolEntry]:
        """全部已注册条目（快照，防并发修改）。"""
        with self._lock:
            return list(self._tools.values())

    def get_all_tool_names(self) -> List[str]:
        """全部已注册工具名（排序）。"""
        return sorted(entry.name for entry in self.get_all_entries())

    def get_toolset_names(self) -> List[str]:
        """全部已注册 toolset 名（排序）。"""
        return sorted({entry.toolset for entry in self.get_all_entries()})

    # -- check_fn 门控（30s TTL / 60s 宽限 / 512 上限 / fail-closed） ----------

    def is_available(self, name: str) -> bool:
        """按 check_fn 判断工具当前是否可用；无 check_fn 恒可用。

        门控语义（对标 Hermes _check_fn_cached）：
        - 结果按 name 缓存 30s（外部状态按分钟级变化，短时复用免探测）；
        - check_fn 失败时，若 60s 内该工具曾成功则放行（last-good 宽限，
          吸收探针抖动，防止断网瞬间工具被静默摘除），且不缓存失败，
          下次调用重新探测；
        - 缓存超 512 条淘汰最旧；
        - check_fn 抛异常按失败处理（fail-closed）。
        """
        entry = self.get_entry(name)
        if entry is None or entry.check_fn is None:
            return True
        now = time.monotonic()
        with self._lock:
            cached = self._gate_cache.get(name)
            if cached is not None and now - cached[0] < _GATE_CACHE_TTL:
                return cached[1]
            self._prune_gate_cache(now)

        try:
            value = bool(entry.check_fn())
        except Exception as e:
            console.warn(f"[工具注册表] {name} 门控探测异常（{e}），按不可用处理")
            value = False

        with self._lock:
            if value:
                self._gate_last_good[name] = now
                self._gate_cache[name] = (now, True)
                return True
            last_good = self._gate_last_good.get(name)
            if last_good is not None and now - last_good < _GATE_GRACE:
                # 上次成功后的瞬时失败：按抖动放行，不缓存失败（下次重探测）
                console.dim(
                    f"[工具注册表] {name} 门控探测失败但在 "
                    f"{int(_GATE_GRACE)}s 宽限内，按可用放行")
                return True
            self._gate_cache[name] = (now, False)
            return False

    def _prune_gate_cache(self, now: float) -> None:
        """淘汰过期条目并限制容量上限（调用方须持有 _lock）。"""
        for key, (ts, _) in list(self._gate_cache.items()):
            if now - ts >= _GATE_CACHE_TTL:
                self._gate_cache.pop(key, None)
        for key, ts in list(self._gate_last_good.items()):
            if now - ts >= _GATE_GRACE:
                self._gate_last_good.pop(key, None)
        while len(self._gate_cache) >= _GATE_CACHE_LIMIT:
            self._gate_cache.pop(next(iter(self._gate_cache)))
        while len(self._gate_last_good) >= _GATE_CACHE_LIMIT:
            self._gate_last_good.pop(next(iter(self._gate_last_good)))

    # -- 定义导出 ------------------------------------------------------------

    def get_definitions(self, tool_names: Optional[List[str]] = None) -> List[dict]:
        """返回 OpenAI Function Calling 格式的工具定义（仅含门控通过者）。

        tool_names 为空时导出全部已注册工具；未注册/门控拒绝的工具跳过。
        """
        result: List[dict] = []
        names = tool_names if tool_names is not None else self.get_all_tool_names()
        for name in sorted(names):
            entry = self.get_entry(name)
            if entry is None or not self.is_available(name):
                continue
            schema = dict(entry.schema)
            schema["name"] = name  # 兜底：确保 schema 带 name 字段
            result.append({"type": "function", "function": schema})
        return result

    # -- 分发 ----------------------------------------------------------------

    @staticmethod
    def _normalize_handler_result(name: str, result: Any) -> str:
        """把 handler 返回值归一化为 JSON 字符串（LLM 可解析协议）。

        - dict/list/基础类型 → json.dumps(ensure_ascii=False)；
        - str → 原样返回（已是字符串）；若非 JSON 仅告警不改写，避免改变既有行为；
        - 无法序列化 → 返回结构化错误 JSON。
        """
        if isinstance(result, str):
            if not _looks_like_json(result):
                console.warn(
                    f"[工具注册表] 工具 '{name}' 返回非 JSON 字符串，"
                    f"模型解析成本升高（建议返回 JSON）")
            return result
        try:
            return json.dumps(result, ensure_ascii=False)
        except (TypeError, ValueError):
            return json.dumps({
                "error": f"工具 {name} 返回值无法序列化为 JSON",
            }, ensure_ascii=False)

    async def dispatch_async(self, name: str, args: dict) -> Optional[str]:
        """异步分发：执行 handler 并返回 JSON 字符串结果。

        未注册返回 None（调用方回退旧路径）；门控拒绝返回结构化错误 JSON
        （fail-closed）；handler 异常返回 {"error": ...} JSON。
        """
        entry = self.get_entry(name)
        if entry is None:
            return None
        # 3.13 全局急停：哨兵文件存在时拒绝高危工具（fail-closed，只影响
        # ESTOP_BLOCKED_TOOLS 内工具，只读工具不受影响）
        if estop.is_blocked(name):
            return json.dumps({
                "error": f"工具「{name}」因全局急停被拒绝（哨兵文件存在）",
            }, ensure_ascii=False)
        if not self.is_available(name):
            return json.dumps({
                "error": f"工具「{name}」当前不可用（环境门控未通过）",
            }, ensure_ascii=False)
        try:
            result = entry.handler(args)
            if inspect.isawaitable(result):
                result = await result
            return self._normalize_handler_result(name, result)
        except Exception as e:
            console.warn(f"[工具注册表] 工具「{name}」执行失败：{e}")
            return json.dumps({
                "error": f"工具「{name}」执行失败：{type(e).__name__}: {e}",
            }, ensure_ascii=False)


def _looks_like_json(text: str) -> bool:
    """宽松判断字符串是否 JSON 形态（{...} / [...] / 纯 JSON 标量）。"""
    stripped = text.strip()
    if not stripped:
        return True
    if stripped[0] not in "{[":  # 纯文本错误信息（如"错误：找不到工具X"）
        return False
    try:
        json.loads(stripped)
        return True
    except ValueError:
        return False


# 进程内单例（模块级，对标 Hermes registry = ToolRegistry()）
tool_registry = ToolRegistry()
