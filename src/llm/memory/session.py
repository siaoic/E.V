"""L3 会话历史（Hermes 式 state.db）：SQLite + FTS5 全文检索。

四层记忆架构的 L3 层：完整保留 Agent 的运行轨迹（不只是聊天记录），
按需召回。落地设计：
- messages 表保存 role / content / tool_call（完整工具调用链）等字段，
  进程内 LLMBrain.history 只裁剪到最近 N 轮，这里存全量轨迹；
- messages_fts 为 external-content FTS5 虚拟表（trigram 分词，支持中文
  子串检索），通过 trigger 与 messages 表自动同步；
- WAL 模式 + busy_timeout，读写不互相阻塞；
- 全部方法同步实现，调用方用 asyncio.to_thread 丢后台线程（不阻塞
  主事件循环），进程内互斥锁保证并发安全。

库文件走 cfg.DATA_ROOT（HISTORY_DB_PATH 派生，默认 <DATA_ROOT>/history.db）。
"""

from __future__ import annotations

import os
import re
import sqlite3
import threading
from datetime import datetime
from typing import Optional

from src.utils import config

# trigram 检索需 ≥3 字符；短查询走 LIKE 兜底
_FTS_MIN_QUERY_LEN = 3
# 检索时剔除 FTS5 语法保留字符（引号/星号/冒号等），防 MATCH 语法错误
_FTS_UNSAFE_RE = re.compile(r"[^\w\u4e00-\u9fff\u3000-\u303f\s]")


def _normalize_for_fts(text: str) -> str:
    """归一化待索引文本：去掉 CJK 与相邻字符间的空格（"B 站" → "B站"）。

    trigram 分词对空白敏感——中文里拉丁字与汉字之间的空格会让「B站直播」
    检索不到「B 站直播」。纯拉丁词之间的空格保留（不破坏英文检索）。
    """
    return re.sub(r"(?<=[\u4e00-\u9fff])\s+|\s+(?=[\u4e00-\u9fff])", "", text or "")


def _session_id() -> str:
    """当前会话标识：按自然日分组（跨进程重启同日续用，人可读）。"""
    return datetime.now().strftime("%Y%m%d")


class SessionStore:
    """会话历史存储（进程内单例）：追加写入 + FTS5 全文检索。"""

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._conn: Optional[sqlite3.Connection] = None
        self._lock = threading.Lock()

    # ---------- 连接与建表 ----------

    def _connect(self) -> sqlite3.Connection:
        if self._conn is None:
            os.makedirs(os.path.dirname(self._db_path) or ".", exist_ok=True)
            conn = sqlite3.connect(self._db_path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._create_schema(conn)
            self._conn = conn
        return self._conn

    @staticmethod
    def _create_schema(conn: sqlite3.Connection) -> None:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL DEFAULT '',
            tool_call TEXT,
            ts TEXT NOT NULL
        );
        -- 兼容旧表：无 fts_content 列时补列（早期 schema 演进保护）
        CREATE INDEX IF NOT EXISTS idx_messages_session
            ON messages(session_id, id);

        -- 全文索引：external-content 模式，trigger 与 messages 表自动同步；
        -- fts_content 列存归一化文本（去 CJK 邻接空格），保证中文子串可检索；
        -- trigram 分词支持中文子串检索（detail=full 为 trigram 必需）
        CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
            fts_content,
            content='messages',
            content_rowid='id',
            tokenize='trigram',
            detail=full
        );

        CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
            INSERT INTO messages_fts(rowid, fts_content)
            VALUES (new.id, new.fts_content);
        END;
        CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
            INSERT INTO messages_fts(messages_fts, rowid, fts_content)
            VALUES ('delete', old.id, old.fts_content);
        END;
        CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE ON messages BEGIN
            INSERT INTO messages_fts(messages_fts, rowid, fts_content)
            VALUES ('delete', old.id, old.fts_content);
            INSERT INTO messages_fts(rowid, fts_content)
            VALUES (new.id, new.fts_content);
        END;
        """)
        conn.commit()
        SessionStore._ensure_column(conn, "messages", "fts_content",
                                    "TEXT NOT NULL DEFAULT ''")
        conn.commit()

    @staticmethod
    def _ensure_column(conn: sqlite3.Connection, table: str, column: str,
                       ddl: str) -> None:
        """旧库缺列时 ALTER TABLE 补列（幂等，不影响已有数据）。"""
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

    # ---------- 写入 ----------

    def add_message(self, session_id: str, role: str, content: str,
                    tool_call: Optional[str] = None) -> int:
        """追加一条消息，返回自增 ID。"""
        with self._lock:
            conn = self._connect()
            cur = conn.execute(
                "INSERT INTO messages(session_id, role, content, tool_call,"
                " fts_content, ts)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (session_id, role, content, tool_call,
                 _normalize_for_fts(content),
                 datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
            conn.commit()
            return int(cur.lastrowid)

    def add_messages(self, session_id: str, rows: list[dict]) -> int:
        """批量追加（单事务），rows 为 [{role, content, tool_call?}]，返回条数。"""
        if not rows:
            return 0
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._lock:
            conn = self._connect()
            conn.executemany(
                "INSERT INTO messages(session_id, role, content, tool_call,"
                " fts_content, ts)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                [(session_id, r.get("role"), r.get("content") or "",
                  r.get("tool_call"),
                  _normalize_for_fts(r.get("content") or ""), now)
                 for r in rows])
            conn.commit()
            return len(rows)

    # ---------- 检索 ----------

    def search(self, query: str, *, limit: int = 20,
               session_id: Optional[str] = None) -> list[dict]:
        """全文检索会话历史（FTS5 优先，trigram 支持中文子串；短查询/失败回退 LIKE）。"""
        query = (query or "").strip()
        if not query:
            return []
        with self._lock:
            conn = self._connect()
            try:
                if len(query) >= _FTS_MIN_QUERY_LEN:
                    return self._fts_search(conn, query, limit, session_id)
            except sqlite3.OperationalError:
                pass  # MATCH 语法/分词不支持 → 回退 LIKE
            return self._like_search(conn, query, limit, session_id)

    def _fts_search(self, conn: sqlite3.Connection, query: str, limit: int,
                    session_id: Optional[str]) -> list[dict]:
        # 归一化（去 CJK 邻接空格）→ 剔除 FTS 保留字符 → 包成短语查询
        # （trigram 按 3 字符子串匹配）
        safe = _FTS_UNSAFE_RE.sub(" ", _normalize_for_fts(query))
        safe = " ".join(safe.split())
        if len(safe) < _FTS_MIN_QUERY_LEN:
            return self._like_search(conn, query, limit, session_id)
        phrase = '"' + safe.replace('"', '""') + '"'
        cond = " AND m.session_id = ?" if session_id else ""
        params: list = [phrase] + ([session_id] if session_id else []) + [limit]
        rows = conn.execute(
            "SELECT m.id, m.session_id, m.role, m.content, m.tool_call, m.ts,"
            " bm25(messages_fts) AS rank"
            " FROM messages_fts"
            " JOIN messages m ON m.id = messages_fts.rowid"
            f" WHERE messages_fts MATCH ?{cond}"
            " ORDER BY rank"
            " LIMIT ?", params).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def _like_search(self, conn: sqlite3.Connection, query: str, limit: int,
                     session_id: Optional[str]) -> list[dict]:
        # LIKE 兜底：转义 % 与 _，防止用户输入当通配符
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        cond = " AND session_id = ?" if session_id else ""
        params: list = [f"%{escaped}%"] + ([session_id] if session_id else []) + [limit]
        rows = conn.execute(
            "SELECT id, session_id, role, content, tool_call, ts"
            " FROM messages"
            f" WHERE content LIKE ? ESCAPE '\\'{cond}"
            " ORDER BY id DESC"
            " LIMIT ?", params).fetchall()
        return [self._row_to_dict(r) for r in rows]

    # ---------- 管理 ----------

    def clear_session(self, session_id: str) -> int:
        """删除指定会话全部历史（FTS 由 trigger 联动），返回删除条数。"""
        with self._lock:
            conn = self._connect()
            cur = conn.execute("DELETE FROM messages WHERE session_id = ?",
                               (session_id,))
            conn.commit()
            return cur.rowcount

    def count(self, session_id: Optional[str] = None) -> int:
        """历史条数（可限定会话）。"""
        with self._lock:
            conn = self._connect()
            if session_id:
                row = conn.execute(
                    "SELECT COUNT(*) AS n FROM messages WHERE session_id = ?",
                    (session_id,)).fetchone()
            else:
                row = conn.execute("SELECT COUNT(*) AS n FROM messages").fetchone()
            return int(row["n"])

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict:
        d = {k: row[k] for k in row.keys() if k != "rank"}
        if "rank" in row.keys():
            d["rank"] = row["rank"]
        return d


# ---------- 进程内单例（懒构建，按当前 cfg 生成） ----------

_instance: Optional[SessionStore] = None
_init_lock = threading.Lock()


def get_session_store() -> SessionStore:
    """返回进程内单例（首次访问按 cfg.HISTORY_DB_PATH 建库）。"""
    global _instance
    if _instance is None:
        with _init_lock:
            if _instance is None:
                _instance = SessionStore(config.cfg.HISTORY_DB_PATH or
                                         os.path.join(config.cfg.DATA_ROOT,
                                                      "history.db"))
    return _instance


def reset_session_store() -> None:
    """清空单例并关闭旧连接（!config memory 热重载时调用）。"""
    global _instance
    with _init_lock:
        if _instance is not None:
            _instance.close()
        _instance = None
