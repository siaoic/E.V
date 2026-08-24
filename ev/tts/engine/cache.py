"""TTS 引擎的磁盘音频缓存：缓存读写 + TTL/容量清理。

缓存是优化不是依赖：任何失败静默降级，不影响正常合成。
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from typing import Optional, Tuple

from ev.utils import console

# ---------- 磁盘音频缓存（相同文本+参考+参数只合成一次） ----------
_TTS_CACHE_SUBDIR = "tts_cache"
_TTS_CACHE_TTL_SEC = 7 * 24 * 3600
_TTS_CACHE_MAX_BYTES = 512 * 1024 * 1024
_CACHE_EVICT_EVERY = 64

_cache_write_count = 0

# 测试通过 monkeypatch.setattr(engine, NAME, VALUE) 覆写常量/函数，
# 其中 engine = ev.tts.engine（包 __init__，re-export 本模块的符号）。
# 拆分后包层与本实现层是两个模块对象，按值导入的 int 覆写不会穿透到这里。
# 统一用 _get(NAME) 查 ev.tts.engine → 回退本文件全局变量，保持测试与生产语义一致。
_SRC_MOD_NAME = "ev.tts.engine"


def _get(name: str):
    """按名查常量/可调用：先查 ev.tts.engine（测试 monkeypatch 落点），再回退本地 globals。"""
    src_mod = sys.modules.get(_SRC_MOD_NAME)
    if src_mod is not None:
        val = getattr(src_mod, name, None)
        if val is not None:
            return val
    return globals()[name]


def _tts_cache_dir() -> str:
    """磁盘缓存目录（<DATA_ROOT>/tts_cache），懒创建；失败返回空串。"""
    fn = _get("_tts_cache_dir")
    if fn is not _tts_cache_dir:
        # 被外部覆写（如 monkeypatch）→ 直接委托
        return fn()
    from ev.utils import config as _config

    root = getattr(_config.cfg, "DATA_ROOT", "") or os.getcwd()
    d = os.path.join(root, _TTS_CACHE_SUBDIR)
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        return ""
    return d


def _tts_cache_key(
    text: str, speaker_audio: str, prompt_audio: str, prompt_text: str,
    _synth_params: Optional[dict] = None,
) -> str:
    """缓存键：文本 + 参考参数 + 合成参数（排序序列化，保证确定性）。"""
    if _synth_params is None:
        from .ref_audio import _SYNTH_PARAMS as _synth
        _synth_params = _synth
    payload = {
        "text": text,
        "speaker_audio": speaker_audio,
        "prompt_audio": prompt_audio,
        "prompt_text": prompt_text,
        **_synth_params,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def _cache_load(key: str) -> Optional[bytes]:
    """读取缓存 wav 字节；TTL 过期 / 读取失败视为未命中（过期即删）。"""
    fn = _get("_cache_load")
    if fn is not _cache_load:
        return fn(key)
    ttl = _get("_TTS_CACHE_TTL_SEC")
    d = _tts_cache_dir()
    if not d:
        return None
    path = os.path.join(d, f"{key}.wav")
    try:
        if time.time() - os.path.getmtime(path) > ttl:
            os.remove(path)
            return None
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return None


def _cache_delete(key: str) -> None:
    """删除单条缓存（退化产物 / 命中后检测异常时清理）。"""
    fn = _get("_cache_delete")
    if fn is not _cache_delete:
        return fn(key)
    d = _tts_cache_dir()
    if not d:
        return
    try:
        os.remove(os.path.join(d, f"{key}.wav"))
    except OSError:
        pass


def _cache_save(key: str, content: bytes, evict_fn=None) -> None:
    """写入缓存：先写临时文件再 os.replace（原子）；周期触发容量清理。"""
    global _cache_write_count
    hook = _get("_cache_save")
    if hook is not _cache_save:
        # 外部覆写：用外部的（注意参数数兼容——测试不传 evict_fn 走默认分支）
        try:
            return hook(key, content)
        except TypeError:
            return hook(key, content, evict_fn)
    if evict_fn is None:
        evict_fn = evict_tts_cache
    d = _tts_cache_dir()
    if not d or not content:
        return
    tmp = os.path.join(d, f"{key}.tmp")
    try:
        with open(tmp, "wb") as f:
            f.write(content)
        os.replace(tmp, os.path.join(d, f"{key}.wav"))
        _cache_write_count += 1
        if _cache_write_count % _CACHE_EVICT_EVERY == 0:
            evict_fn()
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass


def evict_tts_cache() -> Tuple[int, int]:
    """清理磁盘音频缓存：删 TTL 过期文件；超容量按 mtime 淘汰最旧。

    幂等、失败静默。Returns: (删除文件数, 释放字节数)。
    """
    fn = _get("evict_tts_cache")
    if fn is not evict_tts_cache:
        return fn()
    ttl = _get("_TTS_CACHE_TTL_SEC")
    cap_bytes = _get("_TTS_CACHE_MAX_BYTES")
    d = _tts_cache_dir()
    if not d:
        return (0, 0)
    entries: list[Tuple[float, str, int]] = []
    try:
        for name in os.listdir(d):
            if not name.endswith(".wav"):
                continue
            path = os.path.join(d, name)
            try:
                entries.append((os.path.getmtime(path), path, os.path.getsize(path)))
            except OSError:
                continue
    except OSError:
        return (0, 0)
    now = time.time()
    removed: list[str] = []
    freed = 0
    keep: list[Tuple[float, str, int]] = []
    for mtime, path, size in entries:
        if now - mtime > ttl:
            removed.append(path)
            freed += size
        else:
            keep.append((mtime, path, size))
    total = sum(size for _, _, size in keep)
    if total > cap_bytes:
        for mtime, path, size in sorted(keep):
            if total <= cap_bytes:
                break
            removed.append(path)
            freed += size
            total -= size
    for path in removed:
        try:
            os.remove(path)
        except OSError:
            pass
    return (len(removed), freed)


# 兼容 cleaner.py：新引擎临时 wav 播放即删，无持久缓存可清
_wav_cache: dict = {}


def _cleanup_output() -> Tuple[int, int]:
    """兼容 cleaner.py 的旧清理入口：新引擎无 output 目录残留。"""
    return (0, 0)
