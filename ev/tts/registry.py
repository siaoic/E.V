"""TTS provider 注册表（3.14）：按名 / 别名 / 首次可用三层解析。

对标 hermes `agent/tts_registry.py`：register_provider 惰性注册（后写胜出），
resolve_provider 依次尝试 按名 → 别名（大小写不敏感）→ 首次可用 provider，
全部未命中返回 default（不抛错，方便调用方静默降级）。
"""

from typing import Dict, Optional

from ev.tts.provider import GPTSoVITSProvider, TTSProvider

# 默认后端名：E.V 现状只有 GPT-SoVITS，注册表以它为默认
DEFAULT_TTS_PROVIDER = "gpt-sovits"

_REGISTRY: Dict[str, TTSProvider] = {}


def register_provider(provider: TTSProvider) -> None:
    """注册 provider（同名后写胜出）。"""
    _REGISTRY[str(provider.name).strip().lower()] = provider


def get_provider(name: Optional[str] = None) -> Optional[TTSProvider]:
    """按名取 provider（大小写不敏感）；name 为空取默认名。未注册返回 None。"""
    key = (str(name).strip().lower() if name else DEFAULT_TTS_PROVIDER)
    return _REGISTRY.get(key)


def resolve_provider(
    name: Optional[str] = None, default: Optional[TTSProvider] = None
) -> Optional[TTSProvider]:
    """三层解析：按名 → 默认名 → 首次可用，均未命中返回 default。

    name 明确指定时若已注册但不可用（is_available False），不自动换用
    其它后端——避免"悄悄换音色"；只有 name 缺省时才回落首次可用。
    """
    if name:
        p = get_provider(name)
        if p is not None:
            return p
        return default
    p = get_provider(DEFAULT_TTS_PROVIDER)
    if p is not None and p.is_available():
        return p
    for p in _REGISTRY.values():
        if p.is_available():
            return p
    return default


def register_default_provider(engine=None) -> GPTSoVITSProvider:
    """注册默认 GPT-SoVITS provider（幂等，engine 可复用 Application 实例）。"""
    p = get_provider(DEFAULT_TTS_PROVIDER)
    if p is None:
        p = GPTSoVITSProvider(engine)
        register_provider(p)
    return p
