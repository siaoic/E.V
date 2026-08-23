"""ToolRegistry 单元测试（对标 Hermes tools/registry.py 验证点）。

覆盖 3.2 升级验证点：
- 注册 / 跨 toolset 重名拒绝 / override 显式许可；
- check_fn 门控：拒绝、30s TTL 缓存命中、60s last-good 宽限、fail-closed；
- dispatch：dict 归一化为 JSON、未知工具返回 None（回退旧路径）、
  门控拒绝与 handler 异常返回结构化错误 JSON；
- get_definitions 只含门控通过的工具；
- bump_generation 热重载代际递增。
"""
import json
import time

import pytest

from src.agent.tool_registry import ToolRegistry, tool_registry


def make_registry() -> ToolRegistry:
    """每个用例独立的干净注册表（避免单例污染）。"""
    return ToolRegistry()


def handler_ok(args):
    """返回 dict 的 handler（应被归一化为 JSON 字符串）。"""
    return {"ok": True, "echo": args}


class TestRegister:
    def test_register_and_get_entry(self):
        reg = make_registry()
        assert reg.register("a", "local", {"name": "a"}, handler_ok)
        entry = reg.get_entry("a")
        assert entry.toolset == "local"
        assert entry.handler({"x": 1}) == {"ok": True, "echo": {"x": 1}}

    def test_cross_toolset_duplicate_rejected(self):
        reg = make_registry()
        assert reg.register("a", "local", {"name": "a"}, handler_ok)
        assert not reg.register("a", "plugin", {"name": "a"}, handler_ok)
        assert reg.get_entry("a").toolset == "local"  # 原条目未被覆盖

    def test_cross_toolset_override_allowed(self):
        reg = make_registry()
        reg.register("a", "local", {"name": "a"}, handler_ok)
        assert reg.register("a", "plugin", {"name": "a"}, handler_ok, override=True)
        assert reg.get_entry("a").toolset == "plugin"

    def test_same_toolset_re_register_replaces(self):
        reg = make_registry()
        reg.register("a", "local", {"name": "a"}, handler_ok)
        assert reg.register("a", "local", {"name": "a"}, handler_ok)  # 同 toolset 重注册
        assert reg.get_entry("a").toolset == "local"

    def test_deregister(self):
        reg = make_registry()
        reg.register("a", "local", {"name": "a"}, handler_ok)
        reg.deregister("a")
        assert reg.get_entry("a") is None


class TestCheckFn:
    def test_no_check_fn_always_available(self):
        reg = make_registry()
        reg.register("a", "local", {"name": "a"}, handler_ok)
        assert reg.is_available("a") is True

    def test_check_fn_false_rejects(self):
        reg = make_registry()
        reg.register("a", "local", {"name": "a"}, handler_ok,
                     check_fn=lambda: False)
        assert reg.is_available("a") is False

    def test_check_fn_true_allows(self):
        reg = make_registry()
        reg.register("a", "local", {"name": "a"}, handler_ok,
                     check_fn=lambda: True)
        assert reg.is_available("a") is True

    def test_check_fn_exception_fail_closed(self):
        reg = make_registry()
        calls = []

        def flaky():
            calls.append(1)
            raise RuntimeError("probe boom")

        reg.register("a", "local", {"name": "a"}, handler_ok, check_fn=flaky)
        assert reg.is_available("a") is False  # fail-closed

    def test_ttl_cache_hit(self):
        reg = make_registry()
        calls = []

        def probe():
            calls.append(1)
            return False  # 持续不可用：缓存后不再重复探测

        reg.register("a", "local", {"name": "a"}, handler_ok, check_fn=probe)
        assert reg.is_available("a") is False
        assert reg.is_available("a") is False  # 命中 30s TTL 缓存
        assert len(calls) == 1

    def test_last_good_grace(self):
        reg = make_registry()
        state = {"good": True}

        def flaky():
            return state["good"]

        reg.register("a", "local", {"name": "a"}, handler_ok, check_fn=flaky)
        assert reg.is_available("a") is True
        state["good"] = False
        # 上次成功后 60s 内失败 → 宽限放行（不缓存失败，下次重探测）
        assert reg.is_available("a") is True
        assert reg.is_available("a") is True  # 每次都重探测（失败未被缓存）

    def test_grace_expired_rejects(self, monkeypatch):
        reg = make_registry()
        state = {"good": True}

        def flaky():
            return state["good"]

        reg.register("a", "local", {"name": "a"}, handler_ok, check_fn=flaky)
        assert reg.is_available("a") is True
        state["good"] = False
        # 推进时间超过宽限期 → 放行过期，开始拒绝
        from src.agent import tool_registry as tr_module
        monkeypatch.setattr(tr_module, "_GATE_GRACE", 0.0)
        monkeypatch.setattr(tr_module, "_GATE_CACHE_TTL", 0.0)  # 同时让 TTL 缓存失效
        assert reg.is_available("a") is False


class TestDispatch:
    @pytest.mark.asyncio
    async def test_unknown_tool_returns_none(self):
        reg = make_registry()
        assert await reg.dispatch_async("no_such", {}) is None

    @pytest.mark.asyncio
    async def test_dict_result_normalized_to_json(self):
        reg = make_registry()
        reg.register("a", "local", {"name": "a"}, handler_ok)
        result = await reg.dispatch_async("a", {"x": 1})
        assert json.loads(result) == {"ok": True, "echo": {"x": 1}}

    @pytest.mark.asyncio
    async def test_async_handler_supported(self):
        reg = make_registry()

        async def async_impl(args):
            return {"from": "async"}

        reg.register("async_tool", "local", {"name": "async_tool"}, async_impl)
        result = await reg.dispatch_async("async_tool", {})
        assert json.loads(result) == {"from": "async"}

    @pytest.mark.asyncio
    async def test_gated_tool_dispatch_rejected(self):
        reg = make_registry()
        reg.register("a", "local", {"name": "a"}, handler_ok,
                     check_fn=lambda: False)
        result = await reg.dispatch_async("a", {})
        payload = json.loads(result)
        assert "error" in payload  # fail-closed：门控拒绝返回错误 JSON

    @pytest.mark.asyncio
    async def test_handler_exception_returns_error_json(self):
        reg = make_registry()

        def boom(args):
            raise ValueError("kaboom")

        reg.register("a", "local", {"name": "a"}, boom)
        result = await reg.dispatch_async("a", {})
        payload = json.loads(result)
        assert "kaboom" in payload["error"]

    def test_str_result_passthrough(self):
        reg = make_registry()
        # 非 JSON 字符串返回：告警但原样返回（不改写，避免改变既有行为）
        reg.register("a", "local", {"name": "a"}, lambda args: "纯文本结果")
        result = reg._normalize_handler_result("a", "纯文本结果")
        assert result == "纯文本结果"


class TestDefinitions:
    def test_only_available_tools_exported(self):
        reg = make_registry()
        reg.register("on", "local", {"name": "on"}, handler_ok)
        reg.register("off", "local", {"name": "off"}, handler_ok,
                     check_fn=lambda: False)
        defs = reg.get_definitions()
        names = [d["function"]["name"] for d in defs]
        assert names == ["on"]

    def test_definitions_by_name_filter(self):
        reg = make_registry()
        reg.register("a", "local", {"name": "a"}, handler_ok)
        reg.register("b", "local", {"name": "b"}, handler_ok)
        defs = reg.get_definitions(["b"])
        assert [d["function"]["name"] for d in defs] == ["b"]

    def test_schema_name_fallback(self):
        reg = make_registry()
        reg.register("a", "local", {"description": "缺 name"}, handler_ok)
        defs = reg.get_definitions()
        assert defs[0]["function"]["name"] == "a"  # 兜底补 name

    def test_bump_generation(self):
        reg = make_registry()
        g0 = reg.generation
        reg.bump_generation()
        assert reg.generation == g0 + 1


class TestModuleSingleton:
    def test_singleton_registered_local_tools(self):
        # plugins.tools 的 _register_local_tools 会把内置工具注册进单例
        from plugins.tools import _register_local_tools
        _register_local_tools()
        names = tool_registry.get_all_tool_names()
        assert "get_current_time" in names
        assert "write_diary" in names
