"""Cron 作业化加固（升级项 3.9，对标 Hermes cron/jobs.py + scheduler.py）。

四项增强，全部由 AGENT_CRON_HARDEN 开关控制（默认关闭 → 调度行为与现状一致）：
1. 执行账本（executions ledger）：DATA_ROOT/agent_schedule_executions.jsonl 记录每次
   触发（任务 id / due 时刻 / 结果 / 耗时），触发前查重——同一 due 只执行一次，
   防多进程双触发。
2. 跨进程文件锁：tick 处用 msvcrt（Windows）/ fcntl（POSIX）文件锁，拿不到锁即
   跳过本轮；同进程内用 threading.RLock 双保险。
3. 心跳 + 硬中断：JobWatchdog 登记运行中任务并定期续约；check_timeouts() 返回
   超时（默认 3 分钟）任务，运行时统一用 run_bounded_async（3.17）做硬取消。
4. 注入扫描：scan_injection() 对"外部注入段"两档扫描（严格/宽松），命中即告警，
   由调用方决定截断（本项目调度任务为纯用户文本，命中即跳过执行）。
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

from ev.utils import config, console


# 硬中断阈值：任务运行超过该秒数即强制取消（对标 Hermes 3 分钟硬中断）
_HARD_KILL_SECONDS = 180.0
# 心跳续约间隔（后台 watchdog 检查周期）
_HEARTBEAT_INTERVAL = 60.0


def cron_harden_enabled() -> bool:
    """3.9 总开关：关闭时所有加固逻辑跳过，调度行为与现状完全一致。"""
    try:
        return bool(config.cfg.AGENT_CRON_HARDEN)
    except Exception:
        return False


def _ledger_path() -> Path:
    return Path(config.cfg.DATA_ROOT) / "agent_schedule_executions.jsonl"


class ExecutionLedger:
    """执行账本：JSONL 追加写（容错），触发前查重（同一 task_id + due 只执行一次）。"""

    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = Path(path) if path else _ledger_path()
        self._lock = threading.Lock()

    def _load(self) -> list[dict]:
        if not self._path.is_file():
            return []
        try:
            with self._path.open("r", encoding="utf-8") as f:
                return [json.loads(line) for line in f if line.strip()]
        except (OSError, ValueError):
            return []

    def already_executed(self, task_id: str, due_ts: float) -> bool:
        """同一任务的同一 due 时刻是否已执行过（账本查重）。"""
        with self._lock:
            for rec in self._load():
                if str(rec.get("task_id")) == str(task_id) and abs(
                        float(rec.get("due_ts") or 0) - float(due_ts)) < 1.0:
                    return True
        return False

    def record(self, task_id: str, due_ts: float, ok: bool,
               detail: str = "", duration: float = 0.0) -> None:
        """追加一条执行记录（失败静默，不影响调度主流程）。"""
        rec = {
            "task_id": str(task_id),
            "due_ts": float(due_ts),
            "ts": time.time(),
            "ok": bool(ok),
            "detail": str(detail)[:300],
            "duration_ms": int(duration * 1000),
        }
        with self._lock:
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                with self._path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            except OSError as e:
                console.warn(f"[Agent调度] 账本写入失败：{e}")


# ---------------------------------------------------------------------------
# 跨进程文件锁：Windows msvcrt / POSIX fcntl，拿不到锁返回 False（跳过本轮）
# ---------------------------------------------------------------------------

_inproc_lock = threading.RLock()
# 线程级持有计数：同线程重入时直接放行（不重复加 OS 文件锁），
# 最外层退出才真正释放——避免同线程重入触发 msvcrt lock violation
_tick_owner = threading.local()


@contextmanager
def cross_process_tick_lock(path: Optional[Path] = None):
    """跨进程调度锁：同进程 RLock 双保险 + 平台文件锁。

    拿不到文件锁时 yield False（调用方应跳过本轮 tick），拿到则 yield True。
    注意：同一 tick 内任务触发仍走 due_items() 自身逻辑，本锁只防"多进程同时
    取走同一批 due"。
    """
    lock_path = Path(path) if path else (
        Path(config.cfg.DATA_ROOT) / "agent_schedule.tick.lock")
    # 同线程重入（RLock 语义）：已持有则直接放行，避免对同一文件重复加锁
    if getattr(_tick_owner, "count", 0) > 0:
        _tick_owner.count += 1
        try:
            yield True
        finally:
            _tick_owner.count -= 1
        return
    acquired_file = False
    with _inproc_lock:
        try:
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            with lock_path.open("a+b") as f:
                if f.tell() == 0:  # msvcrt 需要文件至少 1 字节
                    f.write(b"\x00")
                    f.flush()
                f.seek(0)
                try:
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired_file = True
                except OSError:
                    acquired_file = False
                if acquired_file:
                    _tick_owner.count = 1
                try:
                    yield acquired_file
                finally:
                    if acquired_file:
                        _tick_owner.count = 0
                        try:
                            if os.name == "nt":
                                import msvcrt
                                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
                            else:
                                import fcntl
                                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                        except OSError:
                            pass
        except OSError as e:
            console.warn(f"[Agent调度] 文件锁异常（跳过本轮）：{e}")
            yield False


# ---------------------------------------------------------------------------
# 心跳 + 硬中断 watchdog：登记运行中任务，超时判定由调用方取消
# ---------------------------------------------------------------------------

class JobWatchdog:
    """运行中任务心跳登记表（线程安全）。

    调用方在任务启动时 register(task_id, task)，执行中周期性 heartbeat(task_id)
    续约；check_timeouts() 返回超时任务清单（供 run_bounded_async 硬取消）。
    """

    def __init__(self, hard_kill_seconds: float = _HARD_KILL_SECONDS) -> None:
        self._jobs: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._hard_kill_seconds = hard_kill_seconds

    def register(self, task_id: str, task: str = "") -> None:
        with self._lock:
            self._jobs[str(task_id)] = {
                "task": str(task),
                "start_ts": time.time(),
                "last_heartbeat": time.time(),
            }

    def heartbeat(self, task_id: str) -> None:
        with self._lock:
            job = self._jobs.get(str(task_id))
            if job is not None:
                job["last_heartbeat"] = time.time()

    def complete(self, task_id: str) -> None:
        with self._lock:
            self._jobs.pop(str(task_id), None)

    def check_timeouts(self, now: Optional[float] = None) -> list[dict]:
        """返回已超时（超过 hard_kill_seconds 未续约）的任务清单。"""
        now = now if now is not None else time.time()
        out = []
        with self._lock:
            for tid, job in list(self._jobs.items()):
                if now - job["last_heartbeat"] > self._hard_kill_seconds:
                    out.append({"task_id": tid, **job})
        return out


# ---------------------------------------------------------------------------
# 注入扫描：两档（严格 / 宽松），命中返回 True 供调用方截断
# ---------------------------------------------------------------------------

# 严格集（纯用户 prompt）：命令注入 / 角色越权 / 标签逃逸
_STRICT_PATTERNS = [
    re.compile(r"ignore\s+(all\s+)?previous", re.IGNORECASE),
    re.compile(r"disregard\s+(all\s+)?(prior|previous)", re.IGNORECASE),
    re.compile(r"(system|developer)\s*prompt", re.IGNORECASE),
    re.compile(r"<{1,2}\|?(system|im_start|user|assistant)\|?>{1,2}",
               re.IGNORECASE),
    re.compile(r"你现在(是|扮演)|请忘记.*(身份|设定|规则)", re.IGNORECASE),
    re.compile(r"(忽略|无视|忘记)\s*(掉)?\s*(之前|以上|先前)?\s*的?\s*"
               r"(所有|全部)?\s*(指令|指示|规则|设定|提示)", re.IGNORECASE),
]
# 宽松集（含 skill markdown 的注入段）：额外放行结构性标记，只拦明显指令覆盖
_RELAXED_PATTERNS = [
    re.compile(r"ignore\s+(all\s+)?previous", re.IGNORECASE),
    re.compile(r"disregard\s+(all\s+)?(prior|previous)", re.IGNORECASE),
]


def scan_injection(text: str, strict: bool = True) -> bool:
    """扫描文本是否含注入指令；命中返回 True（调用方截断该段）。

    strict=True 用于纯用户 prompt（严格集），strict=False 用于含 skill markdown
    的注入段（宽松集，避免把技能里的结构性说明误判为注入）。
    """
    if not text or not text.strip():
        return False
    patterns = _STRICT_PATTERNS if strict else _RELAXED_PATTERNS
    return any(p.search(text) for p in patterns)
