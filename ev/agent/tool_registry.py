"""统一工具注册表：注册 / 门控 / 分发（对标 Hermes tools/registry.py 精简落地）。

解决 3.2 差距：工具分散在 plugins/builtin/tools/ 目录（各工具 index.py 注册）
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

from ev.utils import console
from ev.kernel import estop

# check_fn 门控参数（对标 Hermes tools/registry.py）
_GATE_CACHE_TTL = 30.0     # 门控结果缓存秒数：外部状态按分钟级变化，短时复用免探测
_GATE_GRACE = 60.0         # 上次成功后的宽限秒数：期内瞬时失败按抖动放行（last-good）
_GATE_CACHE_LIMIT = 512    # 门控缓存容量上限：超限淘汰最旧


def _type_ok(value: Any, type_name: str) -> bool:
    """JSON Schema 类型匹配（bool 与 int/float 区分开，避免 True 误判为 number）。"""
    if type_name == "string":
        return isinstance(value, str)
    if type_name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if type_name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if type_name == "boolean":
        return isinstance(value, bool)
    if type_name == "array":
        return isinstance(value, list)
    if type_name == "object":
        return isinstance(value, dict)
    return True  # 未知类型不拦（fail-open）


def _type_label(value: Any) -> str:
    """实际类型名（给 LLM 看的错误信息用）。"""
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, str):
        return "string"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    if value is None:
        return "null"
    return type(value).__name__


def _build_validator(schema: dict) -> Callable[[dict], List[str]]:
    """从 OpenAI function schema 的 parameters 编译参数校验函数。

    支持 JSON Schema 子集（够覆盖现有本地工具 100% 用例）：
    - required：缺失必填参数直接报错；
    - type：string / number / integer / boolean / array / object；
    - enum：取值必须在白名单内；
    - minimum / maximum：数值上下限；
    - array items：元素类型 + object 元素的 required/type 浅校验。
    返回 validate(args) -> List[str]（错误列表，空 = 校验通过）。
    """
    params = (schema or {}).get("parameters") or {}
    props = params.get("properties") or {}
    required = list(params.get("required") or [])

    def validate(args: dict) -> List[str]:
        errors: List[str] = []
        if not isinstance(args, dict):
            return ["参数必须是 JSON 对象"]
        # required 缺失检查（值为 null 也视为缺失）
        for key in required:
            if key not in args or args[key] is None:
                errors.append(f"缺少必填参数「{key}」")
        # 逐个属性：type / enum / 范围 / 数组元素检查
        for key, spec in props.items():
            if key not in args or args[key] is None:
                continue
            value = args[key]
            type_name = spec.get("type")
            if type_name and not _type_ok(value, type_name):
                errors.append(
                    f"参数「{key}」应为 {type_name} 类型，实际是 {_type_label(value)}")
                continue  # 类型已错，其余检查无意义
            enum = spec.get("enum")
            if enum and value not in enum:
                errors.append(f"参数「{key}」取值 {value!r} 不在允许范围 {enum}")
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                minimum = spec.get("minimum")
                maximum = spec.get("maximum")
                if minimum is not None and value < minimum:
                    errors.append(f"参数「{key}」不能小于 {minimum}")
                if maximum is not None and value > maximum:
                    errors.append(f"参数「{key}」不能大于 {maximum}")
            # array 元素浅校验（items：类型 + object 元素的 required/type）
            if type_name == "array" and isinstance(spec.get("items"), dict):
                items = spec["items"]
                item_type = items.get("type")
                item_props = items.get("properties") or {}
                item_required = list(items.get("required") or [])
                for i, item in enumerate(value):
                    if item_type and not _type_ok(item, item_type):
                        errors.append(
                            f"参数「{key}」第 {i + 1} 项应为 {item_type} 类型，"
                            f"实际是 {_type_label(item)}")
                        continue
                    if not isinstance(item, dict):
                        continue
                    for rk in item_required:
                        if rk not in item or item[rk] is None:
                            errors.append(
                                f"参数「{key}」第 {i + 1} 项缺少必填字段「{rk}」")
                    for ik, ispec in item_props.items():
                        if ik not in item or item[ik] is None:
                            continue
                        itype = ispec.get("type")
                        if itype and not _type_ok(item[ik], itype):
                            errors.append(
                                f"参数「{key}」第 {i + 1} 项「{ik}」应为 {itype} 类型")
        return errors

    return validate


def _expand_parameters(parameters: dict) -> dict:
    """dsh 简写参数 → 完整 JSON Schema（L3-C ctx.tools.register 用）。

    {"city": {"type": "string", "required": True, "description": "城市名"}} →
    {"type": "object", "properties": {"city": {"type": "string", "description": "城市名"}},
     "required": ["city"]}
    required 为空时省略该键（与既有 OpenAI def 输出保持一致）。
    """
    props: Dict[str, dict] = {}
    required: List[str] = []
    for key, spec in (parameters or {}).items():
        spec = dict(spec or {})
        if spec.pop("required", False):
            required.append(key)
        props[key] = spec
    schema: Dict[str, Any] = {"type": "object", "properties": props}
    if required:
        schema["required"] = required
    return schema


@dataclass
class ToolEntry:
    """单个已注册工具的元信息。"""
    name: str
    toolset: str
    schema: dict
    handler: Callable[..., Any]        # 调用约定：handler(args: dict) -> Any
    check_fn: Optional[Callable[[], bool]] = None  # 可用性门控（无则恒可用）
    generation: int = 0                # 注册时的代际（热重载判定用）
    validator: Optional[Callable[[dict], List[str]]] = None  # 参数校验（无则放行）
    timeout: float = 10.0              # 参考超时（秒），dsh defineTool 对齐


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
                validator=_build_validator(schema),
            )
            self._generation += 1
            return True

    def register_tool(
        self,
        name: str,
        description: str,
        parameters: dict,
        execute: Callable[..., Any],
        timeout: float = 10.0,
        toolset: str = "local",
        check_fn: Optional[Callable[[], bool]] = None,
        override: bool = True,
    ) -> bool:
        """dsh 风格统一注册（ctx.tools.register 底层实现，L3-C）。

        parameters 用简写格式 {"key": {"type": ..., "required": True}}，
        自动扩展为完整 JSON Schema；execute 为执行函数，调用约定与 handler
        一致（execute(args: dict) -> Any，允许同步/异步）；timeout 为参考
        超时（秒）。本地工具与插件工具经此走同一条注册路径。
        """
        schema = {
            "name": name,
            "description": description,
            "parameters": _expand_parameters(parameters),
        }
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
                handler=execute,
                check_fn=check_fn,
                generation=self._generation,
                validator=_build_validator(schema),
                timeout=timeout,
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

    def validate_args(self, name: str, args: dict) -> List[str]:
        """按注册 schema 校验参数；未注册/无 parameters 返回空列表（fail-open）。

        L2-C：校验不通过时调用方直接转 INVALID_ARGS 结果，不启动真实工具，
        省去无谓的工具启动延迟（减少 LLM 幻觉调用造成的浪费）。
        """
        entry = self.get_entry(name)
        if entry is None or entry.validator is None:
            return []
        return entry.validator(args or {})

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


class ToolContext:
    """ctx.tools 统一接口（L3-C）：插件/本地工具走同一条注册路径。

    对标 dsh apply(ctx) 的 ctx.tools.register：入参
    name / description / parameters（简写）/ execute / timeout（可选），
    屏蔽底层 schema 扩展与注册表细节；同一注册表实例内重名默认覆盖。
    """

    def __init__(self, registry: Optional[ToolRegistry] = None) -> None:
        self._registry = registry if registry is not None else tool_registry

    def register(
        self,
        name: str,
        description: str,
        parameters: dict,
        execute: Callable[..., Any],
        timeout: float = 10.0,
        toolset: str = "local",
        check_fn: Optional[Callable[[], bool]] = None,
        override: bool = True,
    ) -> bool:
        """dsh 风格注册工具；返回是否注册成功（重名被拒时 False）。"""
        return self._registry.register_tool(
            name=name, description=description, parameters=parameters,
            execute=execute, timeout=timeout, toolset=toolset,
            check_fn=check_fn, override=override,
        )

    def get_definitions(self, tool_names: Optional[List[str]] = None) -> List[dict]:
        """当前可用工具定义（OpenAI function calling 格式，门控通过者）。"""
        return self._registry.get_definitions(tool_names)
