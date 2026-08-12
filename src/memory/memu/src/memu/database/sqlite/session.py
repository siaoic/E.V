"""SQLite session manager for database connections."""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import event
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import Session, create_engine

logger = logging.getLogger(__name__)


class SQLiteSessionManager:
    """Handle engine lifecycle and session creation for SQLite store."""

    def __init__(self, *, dsn: str, engine_kwargs: dict[str, Any] | None = None) -> None:
        """Initialize SQLite session manager.

        Args:
            dsn: SQLite connection string (e.g., "sqlite:///path/to/db.sqlite").
            engine_kwargs: Optional keyword arguments for create_engine.
        """
        kw: dict[str, Any] = {
            "connect_args": {"check_same_thread": False},  # Allow multi-threaded access
        }
        if engine_kwargs:
            kw.update(engine_kwargs)
        self._engine = create_engine(dsn, **kw)
        # 开启 WAL 提升并发：读不再被写阻塞；busy_timeout 兜底忙等，
        # 避免 agent 写记忆与检索读并发时出现 "database is locked"。
        @event.listens_for(self._engine, "connect")
        def _set_sqlite_pragma(dbapi_connection, connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.close()

    def session(self) -> Session:
        """Create a new database session."""
        return Session(self._engine, expire_on_commit=False)

    def close(self) -> None:
        """Close the database engine and release resources."""
        try:
            self._engine.dispose()
        except SQLAlchemyError:
            logger.exception("Failed to close SQLite engine")

    @property
    def engine(self) -> Any:
        """Return the underlying SQLAlchemy engine."""
        return self._engine


__all__ = ["SQLiteSessionManager"]
