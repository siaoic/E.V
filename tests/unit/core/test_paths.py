"""数据根路径管理单元测试：集中建目录 + 内置资源首次同步。"""
import pytest

from ev.kernel import paths
from ev.utils import config


@pytest.fixture
def fake_roots(tmp_path, monkeypatch):
    """把 PROJECT_ROOT（含内置知识库种子）与 DATA_ROOT 指到临时目录。"""
    proj = tmp_path / "proj"
    seed = proj / "data" / "knowledge"
    (seed / "curated_cards").mkdir(parents=True)
    (seed / "world_lore").mkdir(parents=True)
    (seed / "facts.yaml").write_text("facts: 1", encoding="utf-8")
    (seed / "persona_lore.md").write_text("lore", encoding="utf-8")
    (seed / "curated_cards" / "01.md").write_text("card", encoding="utf-8")
    monkeypatch.setattr(config.cfg.paths, "PROJECT_ROOT", str(proj))
    monkeypatch.setattr(config.cfg.paths, "DATA_ROOT", str(proj / "data"))
    return proj


def test_ensure_data_dirs_creates_writable_subdirs(tmp_path, monkeypatch):
    data = tmp_path / "data"
    monkeypatch.setattr(config.cfg.paths, "DATA_ROOT", str(data))
    paths.ensure_data_dirs()
    assert (data / "tts_cache").is_dir()
    assert (data / "knowledge" / "curated_cards").is_dir()
    assert (data / "knowledge" / "world_lore").is_dir()


def test_sync_noop_when_same_root(fake_roots):
    # 默认场景：数据根未重定向，源 == 目标 → 跳过，不产生复制
    paths.sync_builtin_resources()
    assert (fake_roots / "data" / "knowledge" / "facts.yaml").is_file()


def test_sync_copies_missing_builtin_resources(fake_roots, monkeypatch):
    new_data = fake_roots / "newdata"
    monkeypatch.setattr(config.cfg.paths, "DATA_ROOT", str(new_data))
    paths.sync_builtin_resources()
    assert (new_data / "knowledge" / "facts.yaml").is_file()
    assert (new_data / "knowledge" / "curated_cards" / "01.md").is_file()


def test_sync_does_not_overwrite_existing(fake_roots, monkeypatch):
    new_data = fake_roots / "newdata"
    dst = new_data / "knowledge" / "facts.yaml"
    dst.parent.mkdir(parents=True)
    dst.write_text("user edited", encoding="utf-8")
    monkeypatch.setattr(config.cfg.paths, "DATA_ROOT", str(new_data))
    paths.sync_builtin_resources()
    assert dst.read_text(encoding="utf-8") == "user edited"


def test_sync_skips_when_no_seed(tmp_path, monkeypatch):
    # 种子不存在（仓库无内置知识库）→ 静默返回，不建目录
    proj = tmp_path / "proj"
    data = tmp_path / "data"
    monkeypatch.setattr(config.cfg.paths, "PROJECT_ROOT", str(proj))
    monkeypatch.setattr(config.cfg.paths, "DATA_ROOT", str(data))
    paths.sync_builtin_resources()
    assert not (data / "knowledge").exists()
