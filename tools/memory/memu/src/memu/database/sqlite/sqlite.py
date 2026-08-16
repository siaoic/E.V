"""SQLite database store implementation for MemU."""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel
from sqlmodel import SQLModel

from memu.database.interfaces import Database
from memu.database.models import (
    RecallFile,
    RecallFileSegment,
    Resource,
)
from memu.database.repositories import (
    RecallFileRepo,
    RecallFileSegmentRepo,
    ResourceRepo,
)
from memu.database.sqlite.repositories.recall_file_repo import SQLiteRecallFileRepo
from memu.database.sqlite.repositories.recall_file_segment_repo import SQLiteRecallFileSegmentRepo
from memu.database.sqlite.repositories.resource_repo import SQLiteResourceRepo
from memu.database.sqlite.schema import SQLiteSQLAModels, get_sqlite_sqlalchemy_models
from memu.database.sqlite.session import SQLiteSessionManager
from memu.database.state import DatabaseState

logger = logging.getLogger(__name__)


# 迁移脚本（按序追加，逐条幂等）：create_all 只建新表、不会给旧表补列，
# 老库（早于当前 schema 版本）在此补列 / 兜底脏数据。
# 单条失败（列已存在 / 表不存在）由 _run_migrations 吞掉并继续，可重复执行。
_MIGRATIONS: list[str] = [
    # 老库升级：补 memU ADR 0003 user 作用域列（旧库缺列时 ORM 查询会崩）
    "ALTER TABLE memu_recall_files ADD COLUMN user_id VARCHAR",
    "ALTER TABLE memu_recall_files ADD COLUMN agent_id VARCHAR",
    "ALTER TABLE memu_recall_files ADD COLUMN user VARCHAR",
    "ALTER TABLE memu_recall_file_segments ADD COLUMN user_id VARCHAR",
    "ALTER TABLE memu_recall_file_segments ADD COLUMN agent_id VARCHAR",
    "ALTER TABLE memu_recall_file_segments ADD COLUMN user VARCHAR",
    "ALTER TABLE memu_resources ADD COLUMN user_id VARCHAR",
    "ALTER TABLE memu_resources ADD COLUMN agent_id VARCHAR",
    "ALTER TABLE memu_resources ADD COLUMN user VARCHAR",
    # created_at 兜底：历史 NULL 补为 updated_at（保证排序 / 时间衰减可用）
    "UPDATE memu_recall_files SET created_at = updated_at WHERE created_at IS NULL",
    "UPDATE memu_recall_file_segments SET created_at = updated_at WHERE created_at IS NULL",
    "UPDATE memu_resources SET created_at = updated_at WHERE created_at IS NULL",
]


class SQLiteStore(Database):
    """SQLite database store implementation.

    This store provides a lightweight, file-based database backend for MemU.
    It uses SQLite for metadata storage and brute-force cosine similarity
    for vector search (native vector support is not available in SQLite).

    Attributes:
        resource_repo: Repository for resource records.
        recall_file_repo: Repository for recall files.
        resources: Dict cache of resource records.
        recall_files: Dict cache of recall file records.
    """

    resource_repo: ResourceRepo
    recall_file_repo: RecallFileRepo
    recall_file_segment_repo: RecallFileSegmentRepo
    resources: dict[str, Resource]
    recall_files: dict[str, RecallFile]
    segments: list[RecallFileSegment]

    def __init__(
        self,
        *,
        dsn: str,
        scope_model: type[BaseModel] | None = None,
        resource_model: type[Any] | None = None,
        recall_file_model: type[Any] | None = None,
        recall_file_segment_model: type[Any] | None = None,
        sqla_models: SQLiteSQLAModels | None = None,
    ) -> None:
        """Initialize SQLite database store.

        Args:
            dsn: SQLite connection string (e.g., "sqlite:///path/to/db.sqlite").
            scope_model: Pydantic model defining user scope fields.
            resource_model: Optional custom resource model.
            recall_file_model: Optional custom recall file model.
            sqla_models: Pre-built SQLAlchemy models container.
        """
        self.dsn = dsn
        self._scope_model: type[BaseModel] = scope_model or BaseModel
        self._scope_fields = list(getattr(self._scope_model, "model_fields", {}).keys())
        self._state = DatabaseState()
        self._sessions = SQLiteSessionManager(dsn=self.dsn)
        self._sqla_models: SQLiteSQLAModels = sqla_models or get_sqlite_sqlalchemy_models(scope_model=self._scope_model)

        # Create tables
        self._create_tables()

        # Use provided models or defaults from sqla_models
        resource_model = resource_model or self._sqla_models.Resource
        recall_file_model = recall_file_model or self._sqla_models.RecallFile
        recall_file_segment_model = recall_file_segment_model or self._sqla_models.RecallFileSegment

        # Initialize repositories
        self.resource_repo = SQLiteResourceRepo(
            state=self._state,
            resource_model=resource_model,
            sqla_models=self._sqla_models,
            sessions=self._sessions,
            scope_fields=self._scope_fields,
        )
        self.recall_file_repo = SQLiteRecallFileRepo(
            state=self._state,
            recall_file_model=recall_file_model,
            sqla_models=self._sqla_models,
            sessions=self._sessions,
            scope_fields=self._scope_fields,
        )
        self.recall_file_segment_repo = SQLiteRecallFileSegmentRepo(
            state=self._state,
            recall_file_segment_model=recall_file_segment_model,
            sqla_models=self._sqla_models,
            sessions=self._sessions,
            scope_fields=self._scope_fields,
        )

        # Set up cache references
        self.resources = self._state.resources
        self.recall_files = self._state.recall_files
        self.segments = self._state.segments

    def _create_tables(self) -> None:
        """Create SQLite tables if they don't exist, then apply idempotent migrations."""
        SQLModel.metadata.create_all(self._sessions.engine)
        self._sqla_models.Base.metadata.create_all(self._sessions.engine)
        self._run_migrations()
        logger.debug("SQLite tables created/verified")

    def _run_migrations(self) -> None:
        """按序执行迁移脚本，单条失败（列已存在 / 表不存在）忽略后继续。

        用异常而非版本号判断是否已迁移，保证可重复执行；单条失败后回滚
        事务再继续下一条，新库表已齐全时全部静默跳过。
        """
        from sqlalchemy import text

        with self._sessions.engine.connect() as conn:
            for sql in _MIGRATIONS:
                try:
                    conn.execute(text(sql))
                except Exception:
                    conn.rollback()
                    logger.debug("迁移跳过（可能已应用）：%s", sql)
            conn.commit()

    def close(self) -> None:
        """Close the database connection and release resources."""
        self._sessions.close()

    def load_existing(self) -> None:
        """Load all existing data from database into cache."""
        self.resource_repo.load_existing()
        self.recall_file_repo.load_existing()
        self.recall_file_segment_repo.load_existing()


__all__ = ["SQLiteStore"]
