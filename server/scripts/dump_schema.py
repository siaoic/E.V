# -*- coding: utf-8 -*-
"""从 data/MaiBot.db 只读导出真实 DDL 到 server/src/db/schema.sql。

用法（在仓库根目录执行）：python server/scripts/dump_schema.py

只读 sqlite_master，不触碰任何用户数据行；导出结果经
generate_drizzle_schema.py 转成 Drizzle schema（src/db/schema.ts）。
"""

from __future__ import annotations

import re
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DB_PATH = REPO_ROOT / "data" / "MaiBot.db"
OUT_PATH = Path(__file__).resolve().parent.parent / "src" / "db" / "schema.sql"


def main() -> int:
    if not DB_PATH.exists():
        print(f"未找到主库: {DB_PATH}（请先在仓库根目录运行过一次 bot.py）", file=sys.stderr)
        return 1

    con = sqlite3.connect(f"file:{DB_PATH.as_posix()}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "SELECT type, name, sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type DESC, name"
        ).fetchall()
    finally:
        con.close()

    tables = [r for r in rows if r[0] == "table" and not r[1].startswith("sqlite_")]
    indexes = [r for r in rows if r[0] == "index" and r[1].startswith("ix_")]

    out: list[str] = [
        f"-- MaiBot.db 真实 DDL（{__import__('datetime').date.today():%Y-%m-%d} 从 data/MaiBot.db 的 sqlite_master 只读导出）",
        "-- 本文件是 TS 侧 Drizzle schema 的单一事实源；勿手改，重导出请用 scripts/dump_schema.py",
        "-- 注意：与 Python 侧的差异仅在于建表语句追加了 IF NOT EXISTS（TS 侧 create-all 兜底用）。",
        "",
        "PRAGMA foreign_keys=ON;",
        "",
    ]
    for _t, _name, sql in sorted(tables, key=lambda r: r[1]):
        ddl = sql.replace("CREATE TABLE ", "CREATE TABLE IF NOT EXISTS ", 1)
        out.append(ddl.rstrip().rstrip(";") + ";")
        out.append("")
    for _i, _name, sql in sorted(indexes, key=lambda r: r[1]):
        ddl = re.sub(
            r"^CREATE\s+(UNIQUE\s+)?INDEX\s+",
            lambda m: f"CREATE {'UNIQUE ' if m.group(1) else ''}INDEX IF NOT EXISTS ",
            sql,
            count=1,
        )
        out.append(ddl.rstrip().rstrip(";") + ";")

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text("\n".join(out), encoding="utf-8")
    print(f"已写出 {OUT_PATH}：{len(tables)} 表 / {len(indexes)} 索引")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
