"""SQLite 元数据存储（复刻 memU 的存储层）。

两张表（与复刻前的 schema 一致）：
- history  ：每条记忆的 ADD/UPDATE/DELETE 变更记录（is_deleted=1 标记删除）
- messages ：每轮对话原始消息（session_scope 归属，供 LLM 提取时的上下文）

向量与实体不在本模块：向量存 ChromaDB collection（payload 含 data/hash/
created_at/updated_at/text_lemmatized/scope 字段），实体存独立 entity
collection（linked_memory_ids 关联记忆）。
"""

from __future__ import annotations

import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = None  # 本模块用 console 输出可观测信息

# 为控制中心展示兼容：datetime 序列化为 ISO 字符串
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class SQLiteManager:
    """history / messages 元数据存储。"""

    def __init__(self, db_path: str = ":memory:"):
        self.db_path = db_path
        self.connection = sqlite3.connect(db_path, check_same_thread=False)
        self._lock = threading.Lock()
        self._create_history_table()
        self._create_messages_table()

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    def _create_history_table(self) -> None:
        with self._lock:
            try:
                self.connection.execute("BEGIN")
                self.connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS history (
                        id           TEXT PRIMARY KEY,
                        memory_id    TEXT,
                        old_memory   TEXT,
                        new_memory   TEXT,
                        event        TEXT,
                        created_at   DATETIME,
                        updated_at   DATETIME,
                        is_deleted   INTEGER,
                        actor_id     TEXT,
                        role         TEXT
                    )
                """
                )
                self.connection.execute("COMMIT")
            except Exception:
                self.connection.execute("ROLLBACK")
                raise

    def _create_messages_table(self) -> None:
        with self._lock:
            try:
                self.connection.execute("BEGIN")
                self.connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS messages (
                        id TEXT PRIMARY KEY,
                        session_scope TEXT,
                        role TEXT,
                        content TEXT,
                        name TEXT,
                        created_at DATETIME
                    )
                """
                )
                self.connection.execute("COMMIT")
            except Exception:
                self.connection.execute("ROLLBACK")
                raise

    # ------------------------------------------------------------------
    # history（add_history / batch_add_history / get_history / reset）
    # ------------------------------------------------------------------

    def add_history(
        self,
        memory_id: str,
        old_memory: Optional[str],
        new_memory: Optional[str],
        event: str,
        *,
        created_at: Optional[str] = None,
        updated_at: Optional[str] = None,
        is_deleted: int = 0,
        actor_id: Optional[str] = None,
        role: Optional[str] = None,
    ) -> None:
        """记录一次记忆变更。"""
        created_at = created_at or _now_iso()
        updated_at = updated_at or created_at
        with self._lock:
            self.connection.execute(
                """
                INSERT OR REPLACE INTO history
                (id, memory_id, old_memory, new_memory, event,
                 created_at, updated_at, is_deleted, actor_id, role)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (str(uuid.uuid4()), memory_id, old_memory, new_memory, event,
                 created_at, updated_at, is_deleted, actor_id, role),
            )
            self.connection.commit()

    def batch_add_history(self, history_records: List[dict]) -> None:
        """批量写 history。"""
        with self._lock:
            for h in history_records:
                self.connection.execute(
                    """
                    INSERT OR REPLACE INTO history
                    (id, memory_id, old_memory, new_memory, event,
                     created_at, updated_at, is_deleted, actor_id, role)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(uuid.uuid4()),
                        h.get("memory_id"),
                        h.get("old_memory"),
                        h.get("new_memory"),
                        h.get("event"),
                        h.get("created_at") or _now_iso(),
                        h.get("updated_at") or h.get("created_at") or _now_iso(),
                        h.get("is_deleted", 0),
                        h.get("actor_id"),
                        h.get("role"),
                    ),
                )
            self.connection.commit()

    def get_history(self, memory_id: str) -> List[Dict[str, Any]]:
        """某条记忆的全部变更记录。"""
        with self._lock:
            rows = self.connection.execute(
                "SELECT * FROM history WHERE memory_id = ? ORDER BY created_at",
                (memory_id,),
            ).fetchall()
            cols = [d[0] for d in self.connection.execute("SELECT * FROM history LIMIT 0").description]
            return [dict(zip(cols, row)) for row in rows]

    # ------------------------------------------------------------------
    # messages（save_messages / get_last_messages）
    # ------------------------------------------------------------------

    def save_messages(self, messages: List[Dict[str, str]], session_scope: str) -> None:
        """保存一轮对话消息。"""
        with self._lock:
            for msg in messages:
                if not isinstance(msg, dict):
                    continue
                self.connection.execute(
                    """
                    INSERT INTO messages
                    (id, session_scope, role, content, name, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(uuid.uuid4()),
                        session_scope,
                        msg.get("role"),
                        msg.get("content"),
                        msg.get("name"),
                        _now_iso(),
                    ),
                )
            self.connection.commit()

    def get_last_messages(self, session_scope: str, limit: int = 10) -> List[Dict[str, Any]]:
        """最近 limit 条消息（供提取上下文）。"""
        with self._lock:
            rows = self.connection.execute(
                """
                SELECT role, content, name FROM messages
                WHERE session_scope = ?
                ORDER BY created_at DESC, rowid DESC LIMIT ?
                """,
                (session_scope, limit),
            ).fetchall()
            return [
                {"role": r[0], "content": r[1], "name": r[2] if r[2] else None}
                for r in reversed(rows)
            ]

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def reset(self) -> None:
        """清空全部表。"""
        with self._lock:
            self.connection.execute("DELETE FROM history")
            self.connection.execute("DELETE FROM messages")
            self.connection.commit()

    def close(self) -> None:
        try:
            self.connection.close()
        except Exception:
            pass


__all__ = ["SQLiteManager"]
