/**
 * SQLite 连接与 schema 兜底。
 *
 * PRAGMA 与 Python 侧 database.py 逐字一致（WAL / cache_size=-64000 /
 * foreign_keys=ON / synchronous=NORMAL / busy_timeout=1000）。
 *
 * 硬规则（迁移风险 R4）：MaiBot.db 只有本 TS 进程打开；Python 内核
 * 服务（A_memorix / TTS / Runner）不得直接打开主库，需要数据时经 HTTP
 * 回调本进程的 capability 接口。
 *
 * DDL 唯一来源是 src/db/schema.sql（真实库只读导出，建表带 IF NOT
 * EXISTS）——本项目不使用 drizzle-kit 迁移，Drizzle 仅做类型化查询。
 */

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";

import Database from "better-sqlite3";
import { drizzle, type BetterSQLite3Database } from "drizzle-orm/better-sqlite3";
import type { FastifyBaseLogger } from "fastify";

import * as schema from "./schema.js";

const SCHEMA_SQL_PATH = path.join(path.dirname(fileURLToPath(import.meta.url)), "schema.sql");

export interface DbHandle {
  sqlite: Database.Database;
  drizzle: BetterSQLite3Database<typeof schema>;
  close(): void;
}

export function openDatabase(dbFile: string, logger: FastifyBaseLogger): DbHandle {
  const sqlite = new Database(dbFile);
  sqlite.pragma("journal_mode = WAL");
  sqlite.pragma("cache_size = -64000");
  sqlite.pragma("foreign_keys = ON");
  sqlite.pragma("synchronous = NORMAL");
  sqlite.pragma("busy_timeout = 1000");
  logger.info({ dbFile }, "SQLite 已连接（WAL 模式）");

  return {
    sqlite,
    drizzle: drizzle(sqlite, { schema }),
    close: () => {
      sqlite.close();
    },
  };
}

/** 兜底建表：执行真实 DDL（全部 IF NOT EXISTS，幂等）。 */
export function ensureSchema(handle: DbHandle, logger: FastifyBaseLogger): void {
  const ddl = readFileSync(SCHEMA_SQL_PATH, "utf8");
  const startedAt = Date.now();
  handle.sqlite.exec(ddl);
  logger.info({ elapsedMs: Date.now() - startedAt }, "数据库模型建表检查完成");
}

const RUNTIME_PERFORMANCE_INDEXES: ReadonlyArray<readonly [string, string]> = [
  [
    "ix_jargons_status_count_id",
    "CREATE INDEX IF NOT EXISTS ix_jargons_status_count_id ON jargons (is_jargon, count DESC, id DESC)",
  ],
  [
    "ix_jargons_global_count_id",
    "CREATE INDEX IF NOT EXISTS ix_jargons_global_count_id ON jargons (is_global, count DESC, id DESC)",
  ],
  [
    "ix_jargons_complete_count_id",
    "CREATE INDEX IF NOT EXISTS ix_jargons_complete_count_id ON jargons (is_complete, count DESC, id DESC)",
  ],
];

/**
 * 运行期性能索引补偿（对应 ensure_runtime_performance_indexes）：
 * 期间临时把 busy_timeout 提到 30s，完成后恢复 1s；被占用时告警并留给下次启动。
 */
export function ensureRuntimePerformanceIndexes(handle: DbHandle, logger: FastifyBaseLogger): void {
  const startedAt = Date.now();
  try {
    handle.sqlite.pragma("busy_timeout = 30000");
    for (const [indexName, statement] of RUNTIME_PERFORMANCE_INDEXES) {
      handle.sqlite.exec(statement);
      logger.debug({ indexName }, "数据库运行期性能索引已检查");
    }
    logger.info({ elapsedMs: Date.now() - startedAt }, "数据库运行期性能索引检查完成");
  } catch (error) {
    logger.warn(
      { error },
      "数据库运行期性能索引检查未完成，当前数据库被占用；下次启动或空闲初始化时会再次检查",
    );
  } finally {
    handle.sqlite.pragma("busy_timeout = 1000");
  }
}
