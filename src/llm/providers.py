"""LLM Provider 注册表（对标 Hermes providers/__init__.py 精简落地）。

解决 3.12 差距：provider 信息（key/base_url/模型）散落在 config / router /
factory 三处，新增 provider 需动多处；无 fallback_models 链、无 aux model 缺省。

本模块提供：
- ProviderProfile dataclass：name / aliases / env_vars / base_url /
  fallback_models / default_aux_model；
- register_provider(profile)：惰性注册表（定义即注册、用时装，不建连接），
  后写胜出（同名 provider 后注册者覆盖）；
- get_provider(name)：按 name 或 alias 解析 Profile；
- iter_fallback_models(name)：按 fallback_models 依次产出降级模型名
  （供运行时故障转移使用，无 fallback 时不产出任何项）。

集成方式（保持 model_router / factory 现有对外签名不变）：
- ModelRouter.service() 未命中 LLM_SERVERS 时查本注册表（name/alias），
  返回含 fallback_models 的服务信息；
- factory.get_openai_client 签名不变（api_key/base_url 由调用方传入，
  Profile 解析后的值经 router 提供给调用方）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, Iterator, List, Optional, Tuple


@dataclass(frozen=True)
class ProviderProfile:
    """一个 LLM 服务商的静态描述（注册后不可变）。"""
    name: str                                    # 唯一标识（小写，如 "deepseek"）
    aliases: Tuple[str, ...] = ()                # 别名（含模型名，如 ("deepseek-chat",)）
    env_vars: Tuple[str, ...] = ()               # API Key 环境变量候选（按序取首个非空）
    base_url: str = ""                           # 服务端点（空串 = SDK 官方默认）
    fallback_models: Tuple[str, ...] = ()        # 主模型失败时依次降级的模型
    default_aux_model: str = ""                  # 后台辅助任务（摘要/判决）缺省模型

    def resolve_api_key(self) -> str:
        """按 env_vars 顺序取首个非空环境变量值；全空返回空串。"""
        for var in self.env_vars:
            value = (os.getenv(var) or "").strip()
            if value:
                return value
        return ""


# 惰性注册表：name → ProviderProfile（后写胜出）
_PROVIDER_REGISTRY: Dict[str, ProviderProfile] = {}


def register_provider(profile: ProviderProfile) -> None:
    """注册一个 Provider Profile；同名后注册者覆盖（后写胜出）。"""
    _PROVIDER_REGISTRY[profile.name] = profile


def get_provider(name: str) -> Optional[ProviderProfile]:
    """按 name 或 alias 解析 Provider Profile；未注册返回 None。"""
    if not name:
        return None
    profile = _PROVIDER_REGISTRY.get(name)
    if profile is not None:
        return profile
    for candidate in _PROVIDER_REGISTRY.values():
        if name in candidate.aliases:
            return candidate
    return None


def get_all_providers() -> List[ProviderProfile]:
    """全部已注册 Profile（按注册顺序）。"""
    return list(_PROVIDER_REGISTRY.values())


def iter_fallback_models(name: str) -> Iterator[str]:
    """依次产出该 provider 的 fallback_models（主模型失败时降级用）。

    无 fallback 或 provider 未注册时不产出任何项（调用方跳过降级）。
    """
    profile = get_provider(name)
    if profile is None:
        return iter(())
    return iter(profile.fallback_models)


def reset_provider_registry() -> None:
    """清空注册表（测试隔离用）。"""
    _PROVIDER_REGISTRY.clear()


# ---------------------------------------------------------------------------
# 存量 provider 预注册（Profile 化，保持 router/factory 对外签名不变）
# ---------------------------------------------------------------------------

def _register_builtin_profiles() -> None:
    """注册 E.V 已知 provider 的 Profile（幂等：已注册跳过）。"""
    _BUILTIN_PROFILES = (
        ProviderProfile(
            name="deepseek",
            aliases=("deepseek-chat", "deepseek-reasoner"),
            env_vars=("DEEPSEEK_API_KEY",),
            base_url="https://api.deepseek.com/v1",
            fallback_models=("deepseek-chat",),
            default_aux_model="deepseek-chat",
        ),
        ProviderProfile(
            name="zhipu",
            aliases=("glm", "bigmodel"),
            env_vars=("ZHIPU_API_KEY", "LLM_API_KEY"),
            base_url="https://open.bigmodel.cn/api/paas/v4",
            fallback_models=("glm-4-flash",),
            default_aux_model="glm-4-flash-250414",
        ),
        ProviderProfile(
            name="openai-compat",
            aliases=("openai", "openai-compatible"),
            env_vars=("OPENAI_API_KEY",),
            base_url="https://api.openai.com/v1",
            fallback_models=(),
            default_aux_model="gpt-4o-mini",
        ),
        ProviderProfile(
            name="qwen",
            aliases=("dashscope", "aliyun"),
            env_vars=("DASHSCOPE_API_KEY", "QWEN_API_KEY"),
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            fallback_models=("qwen-plus",),
            default_aux_model="qwen-turbo",
        ),
    )
    for profile in _BUILTIN_PROFILES:
        if profile.name not in _PROVIDER_REGISTRY:
            register_provider(profile)


_register_builtin_profiles()
