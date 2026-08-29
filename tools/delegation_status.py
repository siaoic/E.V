"""委派任务状态查询工具（优化 7-C）：读 delegation.db 展示后台任务状态。

用法（项目根目录执行）：
    python -m tools.delegation_status            # 默认显示最近 50 条
    python -m tools.delegation_status --limit 10  # 只看最近 10 条
    python -m tools.delegation_status --watch     # 每 5 秒刷新（监控模式）
    python -m tools.delegation_status --json      # JSON 输出（供前端调用）

可在主程序运行时查询（SQLite WAL 模式允许并发读，不阻塞 worker）。
delegation.db 位于 DATA_ROOT/delegation.db（与 async_delegation._db_path 一致）。
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

# 确保项目根在 sys.path（独立运行 python tools/delegation_status.py 时也工作）
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


def _db_path() -> Path:
    """delegation.db 路径：DATA_ROOT/agent/delegation.db。

    import ev.utils.config 时 env_loader 会自动 load_dotenv，
    所以 cfg.DATA_ROOT 已就绪（可用 E_V_DATA_DIR 重定向）。
    """
    try:
        from ev.utils import config
        return Path(config.cfg.DATA_ROOT) / "agent" / "delegation.db"
    except Exception:
        return _PROJECT_ROOT / "data" / "agent" / "delegation.db"


def list_jobs(limit: int = 50) -> list:
    """读 delegation.db 最近 N 条任务（独立只读连接，WAL 允许并发读）。

    返回 [{id, task, status, attempts, created_at, updated_at,
    next_retry, result, error}]，与 DelegationQueue.list_recent 一致。
    """
    db = _db_path()
    if not db.is_file():
        return []
    # 只读 URI 连接：file:...?mode=ro，避免误写 + 与 worker 并发读不冲突
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT id, task, status, attempts, created_at, updated_at, "
            "next_retry, result, error FROM delegate_queue "
            "ORDER BY id DESC LIMIT ?",
            (max(1, min(int(limit), 200)),),
        ).fetchall()
    except sqlite3.Error:
        # 表不存在（worker 未启动过）→ 空列表
        return []
    finally:
        conn.close()
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


# 状态图标（终端友好，对齐 console 风格）
_STATUS_ICON = {
    "pending": "⏳",
    "running": "▶",
    "done": "✅",
    "failed": "❌",
}


def _format_age(ts: float) -> str:
    """相对时间格式化：'just now' / '5m ago' / '2h ago' / '3d ago'。"""
    if not ts:
        return "-"
    diff = max(0, time.time() - ts)
    if diff < 60:
        return "just now"
    if diff < 3600:
        return f"{int(diff // 60)}m ago"
    if diff < 86400:
        return f"{int(diff // 3600)}h ago"
    return f"{int(diff // 86400)}d ago"


def _format_table(jobs: list) -> str:
    """格式化为表格输出（终端显示）。"""
    if not jobs:
        return "（无委派任务记录，delegation.db 为空或不存在）"
    lines = [
        f"{'ID':>6}  {'状态':<8}  {'次数':<6}  {'创建':<10}  {'任务（前 60 字）'}",
        "-" * 80,
    ]
    for j in jobs:
        icon = _STATUS_ICON.get(j["status"], "?")
        task_preview = j["task"][:60].replace("\n", " ")
        if len(j["task"]) > 60:
            task_preview += "…"
        lines.append(
            f"#{j['id']:>5}  {icon} {j['status']:<6}  {j['attempts']:>2}/8  "
            f"{_format_age(j['created_at']):<10}  {task_preview}"
        )
        # 失败或完成时附带结果/错误摘要
        if j["status"] == "failed" and j["error"]:
            lines.append(f"         └ 错误：{j['error'][:80]}")
        elif j["status"] == "done" and j["result"]:
            result_preview = j["result"][:80].replace("\n", " ")
            if len(j["result"]) > 80:
                result_preview += "…"
            lines.append(f"         └ 结果：{result_preview}")
        elif j["status"] == "pending" and j["next_retry"] > 0:
            retry_in = max(0, j["next_retry"] - time.time())
            if retry_in > 0:
                lines.append(f"         └ {int(retry_in)}s 后重试")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="查询后台委派任务状态（读 delegation.db）")
    parser.add_argument("--limit", type=int, default=50,
                        help="显示最近 N 条任务（默认 50，上限 200）")
    parser.add_argument("--watch", action="store_true",
                        help="监控模式：每 5 秒刷新")
    parser.add_argument("--json", action="store_true",
                        help="JSON 输出（供前端调用）")
    args = parser.parse_args()

    if args.json:
        jobs = list_jobs(args.limit)
        print(json.dumps(jobs, ensure_ascii=False, indent=2))
        return

    if args.watch:
        try:
            while True:
                os.system("cls" if os.name == "nt" else "clear")
                jobs = list_jobs(args.limit)
                print(_format_table(jobs))
                print(f"\n[watch] {time.strftime('%H:%M:%S')} | "
                      f"每 5s 刷新 | Ctrl+C 退出")
                time.sleep(5)
        except KeyboardInterrupt:
            print("\n已退出监控")
        return

    jobs = list_jobs(args.limit)
    print(_format_table(jobs))


if __name__ == "__main__":
    main()
