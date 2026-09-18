// DB 契约：PRAGMA 同款 + 真实 DDL 兜底建表（24 表 / 95 索引）+ 运行期索引 + Drizzle 类型化读写

import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";

import { afterAll, beforeAll, describe, expect, it } from "vitest";

import { silentLogger } from "./helpers.js";
import { ensureRuntimePerformanceIndexes, ensureSchema, openDatabase } from "../src/db/client.js";
import { jargons } from "../src/db/schema.js";
import { eq } from "drizzle-orm";

describe("db client", () => {
  const dbFile = path.join(mkdtempSync(path.join(tmpdir(), "maibot-db-test-")), "MaiBot.db");
  const handle = openDatabase(dbFile, silentLogger);

  beforeAll(() => {
    ensureSchema(handle, silentLogger);
    ensureRuntimePerformanceIndexes(handle, silentLogger);
  });

  afterAll(() => {
    handle.close();
  });

  it("PRAGMA 与 Python 侧逐字一致", () => {
    const pragma = (name: string): unknown =>
      handle.sqlite.pragma(name, { simple: true });
    expect(String(pragma("journal_mode")).toLowerCase()).toBe("wal");
    expect(pragma("cache_size")).toBe(-64000);
    expect(Number(pragma("foreign_keys"))).toBe(1);
    expect(Number(pragma("synchronous"))).toBe(1); // 1 = NORMAL
    expect(Number(pragma("busy_timeout"))).toBe(1000);
  });

  it("真实 DDL 兜底建表：24 张表 / 95 个 ix_ 索引", () => {
    const tableCount = handle.sqlite
      .prepare<[], { c: number }>("SELECT COUNT(*) c FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
      .get()!.c;
    const indexCount = handle.sqlite
      .prepare<[], { c: number }>("SELECT COUNT(*) c FROM sqlite_master WHERE type='index' AND name LIKE 'ix_%'")
      .get()!.c;
    expect(tableCount).toBe(24);
    expect(indexCount).toBe(95);
  });

  it("运行期性能索引：3 个 jargons 复合索引已补齐", () => {
    for (const name of ["ix_jargons_status_count_id", "ix_jargons_global_count_id", "ix_jargons_complete_count_id"]) {
      const row = handle.sqlite
        .prepare<[string], { c: number }>("SELECT COUNT(*) c FROM sqlite_master WHERE type='index' AND name=?")
        .get(name)!;
      expect(row.c).toBe(1);
    }
  });

  it("Drizzle 类型化读写：boolean 模式与 0/1 存储互转", () => {
    handle.drizzle.insert(jargons).values({
      content: "绝绝子",
      meaning: "非常极致",
      sessionIdDict: "{}",
      count: 3,
      isComplete: false,
      isGlobal: true,
      lastInferenceCount: 0,
      createdBy: "test",
    }).run();

    const row = handle.drizzle.select().from(jargons).where(eq(jargons.content, "绝绝子")).get();
    expect(row).toBeDefined();
    expect(row!.isGlobal).toBe(true);
    expect(row!.isComplete).toBe(false);

    const raw = handle.sqlite.prepare<[], { is_global: number }>("SELECT is_global FROM jargons WHERE content=?").get("绝绝子")!;
    expect(raw.is_global).toBe(1);
  });
});
