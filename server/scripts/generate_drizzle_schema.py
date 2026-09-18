# -*- coding: utf-8 -*-
"""把 src/db/schema.sql 的真实 DDL 转成 Drizzle schema（src/db/schema.ts）。

用法：python server/scripts/generate_drizzle_schema.py

约定（见迁移调研附录 A）：
- 列名显式携带（蛇形），TS 属性名为驼峰——Drizzle 不带名时会把属性名当列名；
- 时间列是 SQLite DATETIME（ISO 字符串）→ text；
- VARCHAR 无长度语义 → text（长度约束放 zod 层）；
- BOOLEAN 是 0/1 整数 → integer({ mode: "boolean" })（仅 NOT NULL 列启用，避免空值歧义）；
- 索引与 UNIQUE 约束不进 Drizzle schema：DDL 执行以 schema.sql 为准，
  Drizzle 只承担类型化查询，不做迁移。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parent.parent
DDL_PATH = SERVER_DIR / "src" / "db" / "schema.sql"
OUT_PATH = SERVER_DIR / "src" / "db" / "schema.ts"


def split_top_level(body: str) -> list[str]:
    """按顶层逗号切分列定义（括号内的逗号不切，如 UNIQUE (a, b)）。"""
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    for ch in body:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(current).strip())
            current = []
        else:
            current.append(ch)
    tail = "".join(current).strip()
    if tail:
        parts.append(tail)
    return parts


def column_kind(sql_type: str, not_null: bool) -> str:
    """SQLite 类型 → Drizzle 构造器 kind。"""
    upper = sql_type.upper()
    if upper.startswith("BOOLEAN"):
        return "integer_boolean" if not_null else "integer"
    if upper.startswith("INTEGER"):
        return "integer"
    if upper.startswith(("REAL", "FLOAT", "DOUBLE")):
        return "real"
    if upper.startswith("BLOB"):
        return "blob"
    return "text"


def emit_column(kind: str, quoted_name: str) -> str:
    """kind + 蛇形列名 → 带显式列名的构造串。"""
    if kind == "integer_boolean":
        return f'integer({quoted_name}, {{ mode: "boolean" }})'
    return f"{kind}({quoted_name})"


def is_boolean_type(sql_type: str) -> bool:
    return sql_type.upper().startswith("BOOLEAN")


def is_real_type(sql_type: str) -> bool:
    return sql_type.upper().startswith(("REAL", "FLOAT", "DOUBLE"))


def is_integer_type(sql_type: str) -> bool:
    return sql_type.upper().startswith("INTEGER")


def default_chain(raw: str, sql_type: str) -> str:
    raw = raw.strip()
    if raw.upper().startswith("CURRENT_TIMESTAMP"):
        return ""  # 时间默认值由应用层赋值（与 Python 侧 datetime.now 行为对齐）
    if raw.startswith("'") and raw.endswith("'"):
        inner = raw[1:-1]
        if is_boolean_type(sql_type):
            return ".default(true)" if inner in ("1", "true") else ".default(false)"
        if is_integer_type(sql_type):
            return f".default({int(float(inner))})"
        if is_real_type(sql_type):
            return f".default({float(inner)})"
        return f'.default("{inner}")'
    try:
        number = float(raw)
    except ValueError:
        return ""
    if is_integer_type(sql_type):
        return f".default({int(number)})"
    if is_real_type(sql_type):
        return f".default({number})"
    return ""


def snake_to_camel(name: str) -> str:
    parts = name.split("_")
    return parts[0] + "".join(p.title() for p in parts[1:])


def main() -> int:
    if not DDL_PATH.exists():
        print("缺少 schema.sql，请先运行 dump_schema.py", file=sys.stderr)
        return 1
    sql_text = DDL_PATH.read_text(encoding="utf-8")

    table_blocks = re.findall(r"CREATE TABLE IF NOT EXISTS\s+(\w+)\s*\((.*?)\)\s*;", sql_text, re.S)
    if not table_blocks:
        print("schema.sql 中没有表定义", file=sys.stderr)
        return 1

    lines: list[str] = [
        "// 由 scripts/generate_drizzle_schema.py 从 src/db/schema.sql 生成；勿手改。",
        "// 列名显式为蛇形（与真实库一致）；索引与 UNIQUE 约束以 schema.sql 为准。",
        'import { blob, integer, real, sqliteTable, text } from "drizzle-orm/sqlite-core";',
        "",
    ]
    table_names: list[str] = []
    for table_name, body in table_blocks:
        definitions = split_top_level(body)
        primary_key_column: str | None = None
        column_defs: list[tuple[str, str, str]] = []  # (col_name, sql_type, rest)
        unique_constraints: list[str] = []

        for raw_def in definitions:
            definition = raw_def.strip().rstrip(",").strip()
            if not definition:
                continue
            upper = definition.upper()
            if upper.startswith("PRIMARY KEY"):
                cols = re.findall(r"\(([^)]*)\)", definition)
                if cols:
                    primary_key_column = cols[0].strip()
                continue
            if upper.startswith(("UNIQUE", "CONSTRAINT", "FOREIGN KEY")) or re.match(r"^CHECK[\s(]", upper):
                unique_constraints.append(definition)
                continue
            match = re.match(r'^"?(\w+)"?\s+(\w+(?:\([^)]*\))?)(.*)$', definition)
            if match is None:
                continue
            column_defs.append((match.group(1), match.group(2).strip(), match.group(3)))

        entries: list[str] = []
        for col_name, sql_type, rest in column_defs:
            quoted = f'"{col_name}"'
            if re.search(r"\bPRIMARY KEY\b", rest, re.I):
                # 列级主键
                if is_integer_type(sql_type):
                    entries.append(f"    {snake_to_camel(col_name)}: integer({quoted}).primaryKey({{ autoIncrement: true }}),")
                else:
                    kind = column_kind(sql_type, True)
                    entries.append(f"    {snake_to_camel(col_name)}: {emit_column(kind, quoted)}.primaryKey(),")
                continue
            kind = column_kind(sql_type, "NOT NULL" in rest.upper())
            chain = ""
            if "NOT NULL" in rest.upper():
                chain += ".notNull()"
            default_match = re.search(r"\bDEFAULT\s+('(?:[^']*)'|[\w.+-]+)", rest, re.I)
            if default_match:
                chain += default_chain(default_match.group(1), sql_type)
            entries.append(f"    {snake_to_camel(col_name)}: {emit_column(kind, quoted)}{chain},")

        # 表级 PRIMARY KEY：SQLite 的 INTEGER PRIMARY KEY 是 rowid 别名，按自增主键建模
        if primary_key_column is not None:
            pk_def = next((d for d in column_defs if d[0] == primary_key_column), None)
            if pk_def is not None:
                col_name, sql_type, _rest = pk_def
                quoted = f'"{col_name}"'
                if is_integer_type(sql_type):
                    pk_entry = f"    {snake_to_camel(col_name)}: integer({quoted}).primaryKey({{ autoIncrement: true }}),"
                else:
                    kind = column_kind(sql_type, True)
                    pk_entry = f"    {snake_to_camel(col_name)}: {emit_column(kind, quoted)}.primaryKey(),"
                prop = f"{snake_to_camel(col_name)}: "
                entries = [e for e in entries if not e.strip().startswith(prop)]
                entries.insert(0, pk_entry)

        if unique_constraints:
            entries.append("    // 表级约束（以 schema.sql 为准）：")
            entries.extend(f"    //   {constraint}" for constraint in unique_constraints)

        table_names.append(table_name)
        lines.append(f'export const {snake_to_camel(table_name)} = sqliteTable("{table_name}", {{')
        lines.extend(entries)
        lines.append("});")
        lines.append("")

    lines.append("export const allTables = {")
    for table_name in table_names:
        lines.append(f"    {snake_to_camel(table_name)},")
    lines.append("} as const;")
    lines.append("")

    OUT_PATH.write_text("\n".join(lines), encoding="utf-8")
    print(f"已写出 {OUT_PATH}：{len(table_names)} 张表")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
