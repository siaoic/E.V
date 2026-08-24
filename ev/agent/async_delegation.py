"""后台委派队列（升级项 3.8 后半，对标 Hermes tools/async_delegation.py）。

SQLite 持久化队列（DATA_ROOT/delegation.db）+ 常驻后台 worker：长任务
（如"后台查 10 个直播间热度"）离线执行，失败自动重试（最多 8 次 / 48h 过期）。

- enqueue(task)：入队返回 job_id；AGENT_DELEGATE_BACKEND=0 时返回 None，
  调用方走既有同步委派路径（行为不变）。
- start_worker(executor)：启动后台守护线程逐条执行；executor 为可调用对象
  （async 或 sync，接收 job dict 返回结果文本）。
- 状态机：pending → running → done / failed；失败按 next_retry 重试。
"""
from __future__ import annotations

import json
import random
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

from ev.utils import config, console


# 最大重试次数 / 任务 48h 过期（超期不再重试，保留失败记录）
_MAX_ATTEMPTS = 8
_EXPIRE_SECONDS = 48 * 3600
# 后台 worker 轮询间隔（秒）
_POLL_INTERVAL = 5.0


def delegate_backend_enabled() -> bool:
    try:
        return bool(config.cfg.AGENT_DELEGATE_BACKEND)
    except Exception:
        return False


def _db_path() -> Path:
    return Path(config.cfg.DATA_ROOT) / "delegation.db"


class DelegationQueue:
    """SQLite 持久化委派队列（线程安全，check_same_thread=False + 互斥锁）。"""

    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = Path(path) if path else _db_path()
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._create_table()

    def _create_table(self) -> None:
        with self._lock:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS delegate_queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    next_retry REAL DEFAULT 0,
                    result TEXT DEFAULT '',
                    error TEXT DEFAULT ''
                )
                """
            )
            self._conn.commit()

    def enqueue(self, task: str) -> Optional[int]:
        """入队一条后台委派任务；开关关闭时返回 None（调用方走同步路径）。"""
        if not delegate_backend_enabled():
            return None
        if not task or not task.strip():
            return None
        now = time.time()
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO delegate_queue (task, created_at, updated_at) "
                "VALUES (?, ?, ?)",
                (task.strip(), now, now),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def _claim_next(self) -> Optional[dict]:
        """取出一条到期待执行任务并标记 running（幂等：只有 pending 可取）。"""
        now = time.time()
        with self._lock:
            row = self._conn.execute(
                "SELECT id, task FROM delegate_queue "
                "WHERE status='pending' AND next_retry <= ? "
                "ORDER BY id LIMIT 1",
                (now,),
            ).fetchone()
            if row is None:
                return None
            self._conn.execute(
                "UPDATE delegate_queue SET status='running', updated_at=? WHERE id=?",
                (now, int(row[0])),
            )
            self._conn.commit()
            return {"id": int(row[0]), "task": str(row[1])}

    def _finish(self, job_id: int, ok: bool, result: str = "", error: str = "") -> None:
        """执行结束落结果：成功 → done；失败 → 未超重试上限则排期重试。"""
        now = time.time()
        with self._lock:
            row = self._conn.execute(
                "SELECT attempts, created_at FROM delegate_queue WHERE id=?", (job_id,)
            ).fetchone()
            if row is None:
                return
            attempts, created_at = int(row[0]), float(row[1])
            attempts += 1
            if ok:
                status, next_retry, error = "done", 0, ""
            elif attempts >= _MAX_ATTEMPTS or now - created_at > _EXPIRE_SECONDS:
                status, next_retry = "failed", 0
            else:
                # 退避重试：2^attempts 秒 + 抖动，防重试风暴
                delay = min(60.0, (2 ** attempts)) + random.uniform(0, 1.0)
                status, next_retry = "pending", now + delay
            self._conn.execute(
                "UPDATE delegate_queue SET status=?, attempts=?, next_retry=?, "
                "result=?, error=?, updated_at=? WHERE id=?",
                (status, attempts, next_retry, str(result)[:2000], str(error)[:500],
                 now, job_id),
            )
            self._conn.commit()

    def pending_count(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM delegate_queue WHERE status='pending'"
            ).fetchone()
            return int(row[0]) if row else 0

    def list_recent(self, limit: int = 50) -> list:
        """返回最近 N 条任务（按 id 倒序），供 UI / !delegation 命令展示。

        持锁读，与 worker 的写入互斥；limit 上限 200 防一次拉太多。
        """
        limit = max(1, min(int(limit), 200))
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, task, status, attempts, created_at, updated_at, "
                "next_retry, result, error FROM delegate_queue "
                "ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [{
            "id": int(r[0]),
            "task": str(r[1]),
            "status": str(r[2]),
            "attempts": int(r[3]),
            "created_at": float(r[4]),
            "updated_at": float(r[5]),
            "next_retry": float(r[6] or 0),
            "result": str(r[7] or ""),
            "error": str(r[8] or ""),
        } for r in rows]

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass


class DelegationWorker:
    """常驻后台 worker：守护线程轮询队列，逐条执行任务。"""

    def __init__(self, queue: DelegationQueue, executor: Callable[[dict], Any]) -> None:
        self._queue = queue
        self._executor = executor
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._loop, name="delegation-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            job = self._queue._claim_next()
            if job is None:
                self._stop.wait(_POLL_INTERVAL)
                continue
            try:
                result = self._run_one(job)
                self._queue._finish(job["id"], True, result=result or "")
            except Exception as e:
                console.warn(f"[委派后台] 任务 #{job['id']} 失败：{type(e).__name__}: {e}")
                self._queue._finish(job["id"], False, error=str(e))

    def _run_one(self, job: dict) -> str:
        """执行单条任务：先取结果，awaitable 再跑独立事件循环（只调用一次）。"""
        import inspect

        result = self._executor(job)
        if inspect.isawaitable(result):
            import asyncio
            return asyncio.run(result)
        return result


# 模块级单例（进程内一个队列 + 一个 worker）
# 使用 RLock 而非 Lock：ensure_worker() 内部会调用 get_delegation_queue()，
# 在已持有 guard 的同线程内二次 acquire，普通 Lock 会直接死锁。
_queue: Optional[DelegationQueue] = None
_worker: Optional[DelegationWorker] = None
_singleton_guard = threading.RLock()


def get_delegation_queue() -> DelegationQueue:
    global _queue
    with _singleton_guard:
        if _queue is None:
            _queue = DelegationQueue()
        return _queue


def ensure_worker(executor: Callable[[dict], Any]) -> DelegationWorker:
    """确保后台 worker 已启动（幂等）；返回 worker 供 stop。"""
    global _worker
    with _singleton_guard:
        if _worker is None:
            _worker = DelegationWorker(get_delegation_queue(), executor)
            _worker.start()
        return _worker
