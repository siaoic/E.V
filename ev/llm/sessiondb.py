"""会话库（对标 Hermes hermes_state.py + hermes_state_search.py 精简落地）。

3.7：把对话轮次落 SQLite（WAL），提供精确的会话级检索（区别于 memU 的
语义向量召回，用于"精确词/人名/梗"子串命中）。

- messages 表：session_id / role / content / ts；只录 user/assistant 文本
  （工具中间消息不落，对标 hermes 排除 role='tool' 行）；
- FTS5 双索引：messages_fts（unicode61）+ messages_trigram（trigram 中文子串）；
  Python 内置 sqlite3 无法注册自定义 cjk_bigram tokenizer，中文 2 字词
  （如"流萤"）由 LIKE 子串兜底等价召回（trigram 要求查询词 ≥3 字符）；
- 写入：record_turn 仅入队（daemon 后台单线程批量落盘，不阻塞主循环），
  失败静默——落库为旁路，对话流程 100% 不受影响；
- 开关：ENABLE_SESSION_SEARCH=0 时 get_session_db() 返回 None，零开销；
- 搜索四模式（零 LLM 成本）：DISCOVER 关键词 / SCROLL 时间线 / READ 单条 /
  BROWSE 按会话浏览。
"""

from __future__ import annotations

import datetime
import os
import queue
import sqlite3
import threading
from pathlib import Path
from typing import Optional

from ev.utils import config, console

# 只在 user/assistant 两类角色落库（工具中间消息不录）
_INDEXED_ROLES = {"user", "assistant"}

# 后台写入队列上限：超过丢弃最旧（写入是旁路，不允许无限积压内存）
_QUEUE_MAX = 500

# 单条 content 最大落库长度（超出截断，防超大消息撑库）
_MAX_CONTENT = 8000

_SCHEMA_VERSION = 1


class SessionDB:
    """SQLite 会话库：WAL + FTS5 双索引 + 后台批量写入。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False：后台 writer 线程独占写；读走主线程
        self._db = sqlite3.connect(str(self.path), check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._migrate()
        self._queue: "queue.Queue[Optional[tuple]]" = queue.Queue(maxsize=_QUEUE_MAX)
        self._closed = False
        self._writer = threading.Thread(target=self._write_loop, daemon=True)
        self._writer.start()

    # ---------- 建表与迁移 ----------

    def _migrate(self) -> None:
        """版本化建表：messages + FTS5 双索引（外链表，触发器同步）。"""
        with self._db:
            version = self._db.execute("PRAGMA user_version").fetchone()[0]
            if version >= _SCHEMA_VERSION:
                return
            self._db.executescript("""
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    ts TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_messages_session_ts
                    ON messages(session_id, ts);
                CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts
                    USING fts5(content, content='messages', content_rowid='id',
                               tokenize='unicode61');
                CREATE VIRTUAL TABLE IF NOT EXISTS messages_trigram
                    USING fts5(content, content='messages', content_rowid='id',
                               tokenize='trigram');
                -- 外链表触发器：messages 增删时同步 FTS 双索引
                CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
                    INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
                    INSERT INTO messages_trigram(rowid, content) VALUES (new.id, new.content);
                END;
                CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
                    INSERT INTO messages_fts(messages_fts, rowid, content)
                        VALUES ('delete', old.id, old.content);
                    INSERT INTO messages_trigram(messages_trigram, rowid, content)
                        VALUES ('delete', old.id, old.content);
                END;
            """)
            self._db.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")

    # ---------- 写入（后台线程批量落盘） ----------

    def record_turn(self, session_id: str, role: str,
                    content: str, ts: str = "") -> None:
        """入队一条对话轮次（非阻塞；ENABLE_SESSION_SEARCH 关闭时调用方不调本方法）。

        只录 user/assistant；空内容/超长截断；队列满丢弃最旧（旁路不阻塞）。
        """
        role = str(role).strip().lower()
        text = str(content or "").strip()
        if role not in _INDEXED_ROLES or not text:
            return
        if len(text) > _MAX_CONTENT:
            text = text[:_MAX_CONTENT]
        try:
            self._queue.put_nowait((session_id, role, text,
                                    ts or _now_iso()))
        except queue.Full:
            try:
                self._queue.get_nowait()
                self._queue.task_done()
                self._queue.put_nowait((session_id, role, text, ts or _now_iso()))
            except queue.Empty:
                pass

    def _write_loop(self) -> None:
        """daemon 写线程：逐条消费，同一事务攒批提交（失败静默）。

        每条消费的队列项都须 task_done（queue.join 按 put 次数计数），
        否则 flush/close 的 join 会永久挂起。
        """
        while True:
            item = self._queue.get()
            batch = [item]
            while True:
                try:
                    batch.append(self._queue.get_nowait())
                except queue.Empty:
                    break
            try:
                # close 哨兵单独成批（close 先 flush 排空数据再入队 None）
                if None in batch:
                    return
                with self._db:
                    self._db.executemany(
                        "INSERT INTO messages(session_id, role, content, ts) "
                        "VALUES (?, ?, ?, ?)", batch)
            except Exception as e:
                console.warn(f"[SessionDB] 落库失败（旁路，不影响对话）：{e}")
            finally:
                for _ in batch:
                    self._queue.task_done()

    def flush(self) -> None:
        """等待已入队条目全部落盘（测试/关机前调用）。"""
        self._queue.join()

    def close(self) -> None:
        """排空队列并停止写线程（进程退出时调用）。"""
        if self._closed:
            return
        self._closed = True
        try:
            self.flush()  # 先排空数据，哨兵随后单独成批
            self._queue.put_nowait(None)
            self._writer.join(timeout=5.0)
        except (queue.Full, RuntimeError):
            pass
        self._db.close()

    # ---------- 搜索（四模式，零 LLM 成本） ----------

    def search(self, *, query: str = "", mode: str = "DISCOVER",
               session_id: str = "", limit: int = 10,
               before_ts: str = "", msg_id: int = 0) -> dict:
        """按模式查询，返回 {"results": [{"id","session_id","role","content","ts"}]}。

        - DISCOVER：query 关键词（trigram MATCH → LIKE 子串兜底，中文 2 字词可命中）
        - READ：按消息 id 读取单条
        - SCROLL：按会话时间线翻页（session_id + before_ts）
        - BROWSE：列出全部会话概要
        """
        limit = max(1, min(int(limit or 10), 50))
        try:
            if mode == "READ":
                rows = self._db.execute(
                    "SELECT id, session_id, role, content, ts FROM messages "
                    "WHERE id = ?", (msg_id,)).fetchall()
            elif mode == "SCROLL":
                where, args = "session_id = ?", [session_id]
                if before_ts:
                    where += " AND ts < ?"
                    args.append(before_ts)
                rows = self._db.execute(
                    f"SELECT id, session_id, role, content, ts FROM messages "
                    f"WHERE {where} ORDER BY ts DESC LIMIT ?",
                    (*args, limit)).fetchall()
            elif mode == "BROWSE":
                rows = self._db.execute(
                    "SELECT id, session_id, role, content, ts FROM messages "
                    "WHERE session_id = ? ORDER BY ts DESC LIMIT ?",
                    (session_id, limit)).fetchall()
            else:  # DISCOVER
                rows = self._discover(query, limit)
            return {"results": [_row(r) for r in rows]}
        except Exception as e:
            return {"results": [], "error": f"查询失败：{e}"}

    def _discover(self, query: str, limit: int) -> list[tuple]:
        """关键词检索：trigram FTS 优先，短词/中文 2 字词 LIKE 兜底。"""
        q = (query or "").strip()
        if not q:
            return []
        rows = self._fts_match("messages_trigram", q, limit)
        if not rows:
            # trigram 要求查询词 ≥3 字符；短词（如中文 2 字"流萤"）走子串匹配
            like = f"%{_escape_like(q)}%"
            rows = self._db.execute(
                "SELECT id, session_id, role, content, ts FROM messages "
                "WHERE content LIKE ? ESCAPE '\\' ORDER BY id DESC LIMIT ?",
                (like, limit)).fetchall()
        return rows

    def _fts_match(self, table: str, query: str, limit: int) -> list[tuple]:
        """对 FTS 表做短语 MATCH；词长不足/语法不支持时返回空（走 LIKE 兜底）。"""
        q = " ".join(_escape_phrase(w) for w in query.split() if len(w) >= 3)
        if not q:
            return []
        try:
            return self._db.execute(
                f"SELECT m.id, m.session_id, m.role, m.content, m.ts "
                f"FROM {table} f JOIN messages m ON m.id = f.rowid "
                f"WHERE {table} MATCH ? ORDER BY m.id DESC LIMIT ?",
                (q, limit)).fetchall()
        except sqlite3.OperationalError:
            return []


def _row(r: tuple) -> dict:
    return {"id": r[0], "session_id": r[1], "role": r[2],
            "content": r[3], "ts": r[4]}


def _now_iso() -> str:
    return datetime.datetime.now().isoformat()


def _escape_phrase(word: str) -> str:
    """转义 FTS5 短语特殊字符（双引号包裹防语法注入）。"""
    return '"' + word.replace('"', '""') + '"'


def _escape_like(text: str) -> str:
    """转义 LIKE 通配符（% _ \），配合 ESCAPE '\\' 使用。"""
    return (text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_"))


# ---------- 模块级单例（运行时旁路入口） ----------

_db_singleton: Optional[SessionDB] = None
_db_lock = threading.Lock()


def get_session_db() -> Optional[SessionDB]:
    """会话库单例；ENABLE_SESSION_SEARCH 关闭时返回 None（零开销旁路）。"""
    global _db_singleton
    if not bool(config.cfg.ENABLE_SESSION_SEARCH):
        return None
    if _db_singleton is None:
        with _db_lock:
            if _db_singleton is None:
                _db_singleton = SessionDB(
                    os.path.join(config.cfg.DATA_ROOT, "state.db"))
    return _db_singleton


def record_turn_queued(session_id: str, role: str,
                       content: str, ts: str = "") -> None:
    """同步旁路入口（memory.add_turn 调用）：落库失败不影响对话。"""
    try:
        db = get_session_db()
        if db is not None:
            db.record_turn(session_id, role, content, ts)
    except Exception:
        pass


def dump_sessions(max_items: int = 20) -> list[dict]:
    """调试/审计：最近 N 条会话消息（不对外工具暴露）。"""
    db = get_session_db()
    if db is None:
        return []
    try:
        rows = db._db.execute(
            "SELECT id, session_id, role, content, ts FROM messages "
            "ORDER BY id DESC LIMIT ?", (max_items,)).fetchall()
        return [_row(r) for r in rows]
    except Exception:
        return []
