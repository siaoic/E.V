"""TTS 磁盘音频缓存单元测试：md5 键确定性、读写删、TTL 过期、容量淘汰。

缓存目录通过 monkeypatch _tts_cache_dir 指向临时目录，不触网、不依赖配置。
"""
import os
import time

import pytest

from ev.tts import engine


@pytest.fixture
def cache_dir(tmp_path, monkeypatch):
    """把缓存目录指向临时目录，并放宽容量上限便于构造容量淘汰场景。"""
    monkeypatch.setattr(engine, "_tts_cache_dir", lambda: str(tmp_path))
    monkeypatch.setattr(engine, "_TTS_CACHE_MAX_BYTES", 60)  # 60 字节上限
    return tmp_path


class TestCacheKey:
    def test_deterministic(self):
        key1 = engine._tts_cache_key("你好呀", "a.wav", "a.wav", "参考")
        key2 = engine._tts_cache_key("你好呀", "a.wav", "a.wav", "参考")
        assert key1 == key2

    def test_text_differs(self):
        k1 = engine._tts_cache_key("你好呀", "a.wav", "a.wav", "参考")
        k2 = engine._tts_cache_key("再见啦", "a.wav", "a.wav", "参考")
        assert k1 != k2

    def test_ref_differs(self):
        k1 = engine._tts_cache_key("你好呀", "a.wav", "a.wav", "参考")
        k2 = engine._tts_cache_key("你好呀", "b.wav", "b.wav", "参考")
        assert k1 != k2


class TestCacheIO:
    def test_save_load_roundtrip(self, cache_dir):
        key = engine._tts_cache_key("你好呀", "a.wav", "a.wav", "参考")
        engine._cache_save(key, b"RIFFfakewav")
        assert engine._cache_load(key) == b"RIFFfakewav"
        # 落盘文件名是 md5.wav
        assert list(cache_dir.iterdir()) == [cache_dir / f"{key}.wav"]

    def test_load_missing_returns_none(self, cache_dir):
        assert engine._cache_load("deadbeef") is None

    def test_delete_removes(self, cache_dir):
        key = engine._tts_cache_key("你好呀", "a.wav", "a.wav", "参考")
        engine._cache_save(key, b"x")
        engine._cache_delete(key)
        assert engine._cache_load(key) is None

    def test_atomic_tmp_not_leaked(self, cache_dir):
        # 写入路径先写 .tmp 再 replace，最终目录只有正式 wav
        key = engine._tts_cache_key("你好呀", "a.wav", "a.wav", "参考")
        engine._cache_save(key, b"data")
        assert [p.name for p in cache_dir.iterdir()] == [f"{key}.wav"]


class TestCacheEvict:
    def test_ttl_expired_removed_on_read(self, cache_dir, monkeypatch):
        monkeypatch.setattr(engine, "_TTS_CACHE_TTL_SEC", -1)  # 立即过期（确定性）
        key = engine._tts_cache_key("你好呀", "a.wav", "a.wav", "参考")
        engine._cache_save(key, b"data")
        # TTL 为负：读取即判定过期并删除
        assert engine._cache_load(key) is None
        assert not (cache_dir / f"{key}.wav").exists()

    def test_evict_removes_expired(self, cache_dir, monkeypatch):
        monkeypatch.setattr(engine, "_TTS_CACHE_TTL_SEC", -1)  # 全过期
        key = engine._tts_cache_key("你好呀", "a.wav", "a.wav", "参考")
        engine._cache_save(key, b"data")
        removed, freed = engine.evict_tts_cache()
        assert removed >= 1
        assert freed >= 4
        assert not (cache_dir / f"{key}.wav").exists()

    def test_evict_caps_by_oldest(self, cache_dir):
        # 容量上限 60 字节：写入 3 个 40 字节文件后触发淘汰最旧
        key1 = engine._tts_cache_key("一", "a.wav", "a.wav", "参考")
        key2 = engine._tts_cache_key("二", "a.wav", "a.wav", "参考")
        key3 = engine._tts_cache_key("三", "a.wav", "a.wav", "参考")
        for key in (key1, key2, key3):
            engine._cache_save(key, b"x" * 40)
        # 人为把三个文件 mtime 拉开，保证淘汰顺序确定（最旧 = key1）
        paths = [cache_dir / f"{k}.wav" for k in (key1, key2, key3)]
        for i, p in enumerate(paths):
            os.utime(p, (time.time() - (3 - i), time.time() - (3 - i)))
        engine.evict_tts_cache()
        remaining = {p.name for p in cache_dir.iterdir() if p.suffix == ".wav"}
        # 40+40+40=120 > 60 → 至少淘汰 1 个（最旧的 key1）
        assert f"{key1}.wav" not in remaining
        assert remaining <= {f"{key2}.wav", f"{key3}.wav"}
