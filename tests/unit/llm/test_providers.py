"""Provider 注册表单元测试（对标 Hermes providers/__init__.py）。

覆盖 3.12 验证点：
- alias 解析（name/aliases 均可命中）；
- 后写胜出（同名 provider 后注册者覆盖）；
- fallback_models 顺序（iter_fallback_models 依次产出）；
- resolve_api_key 按 env_vars 顺序取首个非空；
- model_router.service 未命中 LLM_SERVERS 时回退注册表。
"""
from types import SimpleNamespace

import pytest

from ev.llm.providers import (
    ProviderProfile,
    get_all_providers,
    get_provider,
    iter_fallback_models,
    register_provider,
    reset_provider_registry,
)


@pytest.fixture(autouse=True)
def clean_registry():
    """每个用例独立注册表：清空后重建内置 Profile，互不污染。"""
    from ev.llm.providers import _register_builtin_profiles
    reset_provider_registry()
    _register_builtin_profiles()
    yield
    reset_provider_registry()


class TestRegisterAndResolve:
    def test_register_and_get_by_name(self):
        p = ProviderProfile(name="foo", aliases=("foo-chat",),
                            base_url="https://x/v1",
                            fallback_models=("foo-pro", "foo-flash"))
        register_provider(p)
        assert get_provider("foo") is p

    def test_get_by_alias(self):
        p = ProviderProfile(name="foo", aliases=("foo-chat",))
        register_provider(p)
        assert get_provider("foo-chat") is p

    def test_unknown_returns_none(self):
        assert get_provider("no-such") is None
        assert get_provider("") is None

    def test_last_write_wins(self):
        p1 = ProviderProfile(name="foo", base_url="https://old")
        p2 = ProviderProfile(name="foo", base_url="https://new")
        register_provider(p1)
        register_provider(p2)
        assert get_provider("foo").base_url == "https://new"  # 后写胜出


class TestFallback:
    def test_fallback_models_order(self):
        p = ProviderProfile(name="foo", aliases=("foo-chat",),
                            fallback_models=("m1", "m2", "m3"))
        register_provider(p)
        assert list(iter_fallback_models("foo-chat")) == ["m1", "m2", "m3"]

    def test_no_fallback_empty(self):
        p = ProviderProfile(name="foo")
        register_provider(p)
        assert list(iter_fallback_models("foo")) == []

    def test_unknown_provider_no_fallback(self):
        assert list(iter_fallback_models("ghost")) == []


class TestEnvResolution:
    def test_resolve_api_key_first_non_empty(self, monkeypatch):
        monkeypatch.setenv("A_KEY", "")
        monkeypatch.setenv("B_KEY", "real-key")
        p = ProviderProfile(name="foo", env_vars=("A_KEY", "B_KEY"))
        assert p.resolve_api_key() == "real-key"

    def test_resolve_api_key_missing(self, monkeypatch):
        monkeypatch.delenv("A_KEY", raising=False)
        p = ProviderProfile(name="foo", env_vars=("A_KEY",))
        assert p.resolve_api_key() == ""


class TestBuiltinProfiles:
    def test_builtin_profiles_registered(self):
        names = {p.name for p in get_all_providers()}
        assert {"deepseek", "zhipu", "openai-compat", "qwen"}.issubset(names)

    def test_builtin_zhipu_alias(self):
        assert get_provider("glm") is not None  # zhipu 的别名


class TestRouterIntegration:
    def test_service_falls_back_to_registry(self, monkeypatch):
        from ev.llm.utils import model_router
        monkeypatch.setattr(
            model_router, "_parse_servers", lambda: [])  # 无 LLM_SERVERS 配置
        router = model_router.ModelRouter()
        # deepseek 是内置注册 Profile：service 未命中 LLM_SERVERS 时回退注册表
        service = router.service("deepseek")
        assert service is not None
        assert service["name"] == "deepseek"
        assert "api.deepseek.com" in service["base_url"]
        assert service["fallback_models"]  # 内置 deepseek 有 fallback

    def test_service_unknown_returns_none(self, monkeypatch):
        from ev.llm.utils import model_router
        monkeypatch.setattr(
            model_router, "_parse_servers", lambda: [])
        router = model_router.ModelRouter()
        assert router.service("ghost-service") is None  # 未注册 → 保持旧行为

    def test_service_prefers_configured_servers(self, monkeypatch):
        from ev.llm.utils import model_router
        servers = [{"name": "deepseek", "base_url": "https://custom",
                    "api_key": "k", "model": "m"}]
        monkeypatch.setattr(model_router, "_parse_servers", lambda: servers)
        router = model_router.ModelRouter()
        service = router.service("deepseek")
        assert service["base_url"] == "https://custom"  # LLM_SERVERS 优先
