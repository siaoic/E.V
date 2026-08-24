"""memU SQLite 迁移机制单元测试：老库补 scope 列 + created_at 兜底。"""
import sqlite3
import sys
from pathlib import Path

import pydantic
import pytest

_MEMU_SRC = Path(__file__).resolve().parents[3] / "tools" / "memory" / "memu" / "src"

pytestmark = pytest.mark.skipif(
    not _MEMU_SRC.is_dir(), reason="memU 子项目目录缺失")


@pytest.fixture
def memu_importable():
    """把 memU 引擎源码目录加入 sys.path（与 tools/memory/memory.py 一致）。

    并重置 schema._MODEL_CACHE + models 模块：其他测试（如 test_agent_loop）
    可能用不同 scope 类触发 build_sqlite_table_model，导致同一 Column 对象
    重复绑定到同名 Table（"Column 'url' already assigned"）。
    通过 importlib.reload 重建模型类 → 生成全新的 Column 实例。
    """
    import importlib
    if str(_MEMU_SRC) not in sys.path:
        sys.path.insert(0, str(_MEMU_SRC))
    # 清缓存 + reload models 模块 → Column 对象重建
    try:
        from memu.database.sqlite import schema, models
        schema._MODEL_CACHE.clear()
        importlib.reload(models)
        importlib.reload(schema)
    except Exception:
        pass
    yield _MEMU_SRC
    # teardown 再清一次
    try:
        from memu.database.sqlite import schema, models
        schema._MODEL_CACHE.clear()
        importlib.reload(models)
        importlib.reload(schema)
    except Exception:
        pass


def _make_old_db(path: Path) -> None:
    """构造早于当前 schema 的旧库：无 scope 列，且 created_at 为 NULL。"""
    con = sqlite3.connect(str(path))
    con.executescript(
        """
        CREATE TABLE memu_recall_files (
            id VARCHAR NOT NULL PRIMARY KEY,
            created_at DATETIME,
            updated_at DATETIME NOT NULL,
            name VARCHAR NOT NULL,
            track VARCHAR DEFAULT 'memory' NOT NULL,
            description TEXT NOT NULL,
            embedding JSON,
            content TEXT
        );
        INSERT INTO memu_recall_files
            (id, created_at, updated_at, name, track, description)
            VALUES ('m1', NULL, '2026-01-01 00:00:00', 'n1', 'memory', 'd');
        """
    )
    con.commit()
    con.close()


def _make_store(dsn: str):
    """按项目实际 scope 模型（user_id / agent_id / user）构造 SQLiteStore。

    用模块级单例 scope 模型：memU 的 _MODEL_CACHE 按 scope 类对象缓存，
    每次新建同类会重建 SQLModel 表模型导致列冲突，故全程复用同一类。
    """
    from memu.database.sqlite.sqlite import SQLiteStore

    return SQLiteStore(dsn=dsn, scope_model=_VtuberScope)


class _VtuberScope(pydantic.BaseModel):
    user_id: str | None = None
    agent_id: str | None = None
    user: str | None = None


def test_migrations_backfill_old_db(tmp_path, memu_importable):
    db = tmp_path / "old.db"
    _make_old_db(db)
    _make_store(f"sqlite:///{db}")

    con = sqlite3.connect(str(db))
    cols = {r[1] for r in con.execute("PRAGMA table_info(memu_recall_files)")}
    assert {"user_id", "agent_id", "user"} <= cols  # scope 列已补
    created_at = con.execute(
        "SELECT created_at FROM memu_recall_files WHERE id='m1'").fetchone()[0]
    assert created_at is not None  # NULL created_at 已兜底为 updated_at
    con.close()


def test_migrations_idempotent(tmp_path, memu_importable):
    # 全新库重复打开：迁移逐条幂等不报错
    db = tmp_path / "fresh.db"
    _make_store(f"sqlite:///{db}")
    _make_store(f"sqlite:///{db}")

    con = sqlite3.connect(str(db))
    cols = {r[1] for r in con.execute("PRAGMA table_info(memu_recall_files)")}
    assert {"user_id", "agent_id", "user"} <= cols
    con.close()


def test_migrations_add_missing_tables_too(tmp_path, memu_importable):
    # 旧库只缺 recall_files 表之外的两张表 → 迁移跳过不存在的表，create_all 补齐
    db = tmp_path / "partial.db"
    con = sqlite3.connect(str(db))
    con.executescript(
        """
        CREATE TABLE memu_recall_files (
            id VARCHAR NOT NULL PRIMARY KEY,
            created_at DATETIME,
            updated_at DATETIME NOT NULL,
            name VARCHAR NOT NULL,
            track VARCHAR DEFAULT 'memory' NOT NULL,
            description TEXT NOT NULL,
            embedding JSON,
            content TEXT
        );
        """
    )
    con.commit()
    con.close()
    _make_store(f"sqlite:///{db}")

    con = sqlite3.connect(str(db))
    tables = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"memu_recall_files", "memu_recall_file_segments", "memu_resources"} <= tables
    con.close()
