/**
 * 人物信息管理路由（对照 src/webui/routers/person.py 逐端点迁移）。
 *
 * 响应形状与 FastAPI 版逐字对齐：
 * - 时间字段：SQLite DATETIME（ISO 字符串）→ epoch 秒（Python `.timestamp()`，
 *   按本地时区解释，无值时 null）；
 * - 列表排序：`last_known_time` 倒序、NULL 最后；
 * - PATCH 为「显式提供的字段才更新」（exclude_unset 语义），空更新 400。
 */

import { desc, eq, like, or, sql } from "drizzle-orm";
import { z } from "zod";
import type { FastifyInstance } from "fastify";

import type { DbHandle } from "../../db/client.js";
import { personInfo } from "../../db/schema.js";

const listQuerySchema = z.object({
  page: z.coerce.number().int().min(1).default(1),
  page_size: z.coerce.number().int().min(1).max(100).default(20),
  search: z.string().optional(),
  is_known: z
    .string()
    .optional()
    .transform((value) => (value === undefined ? undefined : value === "true")),
  platform: z.string().optional(),
  user_id: z.string().optional(),
});

const updateBodySchema = z
  .object({
    person_name: z.string().nullable().optional(),
    name_reason: z.string().nullable().optional(),
    nickname: z.string().nullable().optional(),
    memory_points: z.string().nullable().optional(),
    is_known: z.boolean().optional(),
  })
  .strict();

const batchDeleteBodySchema = z.object({ person_ids: z.array(z.string()) });

type PersonRow = typeof personInfo.$inferSelect;

/** SQLite DATETIME（"YYYY-MM-DD HH:MM:SS[.fff]"，本地时区）→ epoch 秒。 */
function toEpochSeconds(value: string | null): number | null {
  if (!value) {
    return null;
  }
  const normalized = value.includes("T") ? value : value.replace(" ", "T");
  const parsed = new Date(normalized);
  if (Number.isNaN(parsed.getTime())) {
    return null;
  }
  return Math.floor(parsed.getTime() / 1000);
}

function parseGroupNickName(raw: string | null): Array<Record<string, string>> | null {
  if (!raw) {
    return null;
  }
  try {
    return JSON.parse(raw) as Array<Record<string, string>>;
  } catch {
    return null;
  }
}

function personToResponse(person: PersonRow) {
  return {
    id: person.id ?? 0,
    is_known: person.isKnown === true,
    person_id: person.personId,
    person_name: person.personName,
    name_reason: person.nameReason,
    platform: person.platform,
    user_id: person.userId,
    nickname: person.userNickname,
    group_nick_name: parseGroupNickName(person.groupCardname),
    memory_points: person.memoryPoints,
    know_times: person.knowCounts,
    know_since: toEpochSeconds(person.firstKnownTime),
    last_know: toEpochSeconds(person.lastKnownTime),
  };
}

export function registerPersonRoutes(app: FastifyInstance, db: DbHandle): void {
  const { drizzle } = db;

  app.get("/api/webui/person/list", async (request, reply) => {
    const parsed = listQuerySchema.safeParse(request.query);
    if (!parsed.success) {
      return reply.code(422).send({ detail: parsed.error.issues[0]?.message ?? "请求不合法" });
    }
    const { page, page_size, search, is_known, platform, user_id } = parsed.data;

    const filters = [];
    if (search) {
      const pattern = `%${search}%`;
      filters.push(
        or(
          like(personInfo.personName, pattern),
          like(personInfo.userNickname, pattern),
          like(personInfo.userId, pattern),
        ),
      );
    }
    if (is_known !== undefined) {
      filters.push(eq(personInfo.isKnown, is_known));
    }
    if (platform) {
      filters.push(eq(personInfo.platform, platform));
    }
    if (user_id) {
      filters.push(eq(personInfo.userId, user_id));
    }
    const where = filters.length > 0 ? (filters.length === 1 ? filters[0] : or(...filters)) : undefined;

    const rows = drizzle
      .select()
      .from(personInfo)
      .where(where)
      .orderBy(sql`${personInfo.lastKnownTime} IS NULL`, desc(personInfo.lastKnownTime))
      .limit(page_size)
      .offset((page - 1) * page_size)
      .all();
    const total = drizzle
      .select({ count: sql<number>`count(*)` })
      .from(personInfo)
      .where(where)
      .get()!.count;

    return {
      success: true,
      total: Number(total),
      page,
      page_size,
      data: rows.map((row) => personToResponse(row)),
    };
  });

  app.get("/api/webui/person/stats/summary", async () => {
    const total = Number(
      drizzle.select({ count: sql<number>`count(*)` }).from(personInfo).get()!.count,
    );
    const known = Number(
      drizzle
        .select({ count: sql<number>`count(*)` })
        .from(personInfo)
        .where(eq(personInfo.isKnown, true))
        .get()!.count,
    );
    const platforms: Record<string, number> = {};
    for (const row of drizzle.select({ platform: personInfo.platform }).from(personInfo).all()) {
      if (row.platform) {
        platforms[row.platform] = (platforms[row.platform] ?? 0) + 1;
      }
    }
    return { success: true, data: { total, known, unknown: total - known, platforms } };
  });

  app.get("/api/webui/person/:person_id", async (request, reply) => {
    const { person_id } = request.params as { person_id: string };
    const row = drizzle.select().from(personInfo).where(eq(personInfo.personId, person_id)).limit(1).get();
    if (!row) {
      return reply.code(404).send({ detail: `未找到 ID 为 ${person_id} 的人物信息` });
    }
    return { success: true, data: personToResponse(row) };
  });

  app.patch("/api/webui/person/:person_id", async (request, reply) => {
    const { person_id } = request.params as { person_id: string };
    const parsed = updateBodySchema.safeParse(request.body ?? {});
    if (!parsed.success) {
      return reply.code(422).send({ detail: parsed.error.issues[0]?.message ?? "请求不合法" });
    }
    // exclude_unset 语义：只更新「显式出现在请求体里」的字段（显式 null 也算提供）
    const provided = Object.keys(parsed.data).filter(
      (key) => (request.body as Record<string, unknown> | null)?.[key] !== undefined,
    );
    if (provided.length === 0) {
      return reply.code(400).send({ detail: "未提供任何需要更新的字段" });
    }

    const row = drizzle.select().from(personInfo).where(eq(personInfo.personId, person_id)).limit(1).get();
    if (!row) {
      return reply.code(404).send({ detail: `未找到 ID 为 ${person_id} 的人物信息` });
    }

    const body = request.body as Record<string, unknown>;
    const updates: Record<string, unknown> = {};
    if ("person_name" in body) {
      updates.personName = body.person_name ?? null;
    }
    if ("name_reason" in body) {
      updates.nameReason = body.name_reason ?? null;
    }
    if ("nickname" in body) {
      updates.userNickname = body.nickname ?? null;
    }
    if ("memory_points" in body) {
      updates.memoryPoints = body.memory_points ?? null;
    }
    if ("is_known" in body) {
      updates.isKnown = body.is_known === true;
    }
    // 与 Python 一致：任何更新都刷新 last_known_time（SQLite DATETIME 存储格式）
    updates.lastKnownTime = new Date().toLocaleString("sv-SE").replace("T", " ");

    drizzle
      .update(personInfo)
      .set(updates as Partial<typeof personInfo.$inferInsert>)
      .where(eq(personInfo.personId, person_id))
      .run();
    const updated = drizzle.select().from(personInfo).where(eq(personInfo.personId, person_id)).limit(1).get()!;

    return {
      success: true,
      message: `成功更新 ${provided.length} 个字段`,
      data: personToResponse(updated),
    };
  });

  app.delete("/api/webui/person/:person_id", async (request, reply) => {
    const { person_id } = request.params as { person_id: string };
    const row = drizzle.select().from(personInfo).where(eq(personInfo.personId, person_id)).limit(1).get();
    if (!row) {
      return reply.code(404).send({ detail: `未找到 ID 为 ${person_id} 的人物信息` });
    }
    const displayName = row.personName || row.userNickname || row.userId;
    drizzle.delete(personInfo).where(eq(personInfo.personId, person_id)).run();
    return { success: true, message: `成功删除人物信息: ${displayName}` };
  });

  app.post("/api/webui/person/batch/delete", async (request, reply) => {
    const parsed = batchDeleteBodySchema.safeParse(request.body ?? {});
    if (!parsed.success || parsed.data.person_ids.length === 0) {
      return reply.code(400).send({ detail: "未提供要删除的人物ID" });
    }
    let deletedCount = 0;
    const failedIds: string[] = [];
    for (const personId of parsed.data.person_ids) {
      const exists = drizzle
        .select({ id: personInfo.id })
        .from(personInfo)
        .where(eq(personInfo.personId, personId))
        .limit(1)
        .get();
      if (exists) {
        drizzle.delete(personInfo).where(eq(personInfo.personId, personId)).run();
        deletedCount += 1;
      } else {
        failedIds.push(personId);
      }
    }
    const failedCount = failedIds.length;
    let message = `成功删除 ${deletedCount} 个人物`;
    if (failedCount > 0) {
      message += `，${failedCount} 个失败`;
    }
    return { success: true, message, deleted_count: deletedCount, failed_count: failedCount, failed_ids: failedIds };
  });
}
