/**
 * 表达方式管理路由（对照 src/webui/routers/expression.py 逐端点迁移）。
 *
 * 关键语义：
 * - 可见性：表达聊天流 ∩ 当前账号对（bot_platform_accounts 表 + 配置
 *   bot.platforms + webui 伪账号）。Python 侧另有运行期 observed 平台集合，
 *   TS 侧以落库与配置为准（差异已记录在迁移文档）；
 * - 有效黑话式列表排序：last_active_time NULLS LAST 倒序；
 * - 审核筛选：user_checked = checked 且 modified_by=USER；unchecked = !checked；
 * - 批量审核：拒绝 = 直接删除记录；通过 = checked=true + modified_by=USER；
 * - AI 审核日志：logs/expression_review/review_logs.json，救回经 review_log_id 关联。
 *
 * 显式 501（6 个）：/groups、/clusters/* 依赖配置表达组通配符展开
 * （ChatConfigUtils）与向量索引 payload（下一批）；/legacy-import/* 依赖
 * chat_manager 运行时会话解析（resolve_sessions_by_target），按 D1 边界
 * 保留在 Python 内核侧。
 */

import { readFileSync } from "node:fs";
import path from "node:path";

import { and, desc, eq, gte, inArray, isNotNull, isNull, like, not, or, sql, type SQL } from "drizzle-orm";
import { parse as parseToml } from "smol-toml";
import { z } from "zod";
import type { FastifyInstance, FastifyReply } from "fastify";

import type { DbHandle } from "../../db/client.js";
import { sqliteDatetime } from "../../db/datetime.js";
import { chatSessions, expressions } from "../../db/schema.js";
import type { ExpressionReviewStore } from "../../services/expression-review-store.js";

type ExpressionRow = typeof expressions.$inferSelect;
type ChatSessionRow = typeof chatSessions.$inferSelect;

export interface ExpressionRouteDeps {
  db: DbHandle;
  rootDir: string;
  reviewStore: ExpressionReviewStore;
}

const pad = (value: number, width = 2) => String(value).padStart(width, "0");

function toEpochSeconds(value: string | null): number | null {
  if (!value) {
    return null;
  }
  const parsed = new Date(value.replace(" ", "T"));
  return Number.isNaN(parsed.getTime()) ? null : Math.floor(parsed.getTime() / 1000);
}

function isoFormat(value: string | null): string | null {
  if (!value) {
    return null;
  }
  return value.replace(" ", "T");
}

function parseExportDatetime(value: string | null | undefined): string {
  if (!value) {
    return sqliteDatetime();
  }
  const normalized = value.includes("T") ? value.replace("T", " ") : value;
  const parsed = new Date(normalized.trim());
  return Number.isNaN(parsed.getTime()) ? sqliteDatetime() : sqliteDatetime(parsed);
}

function parseModifiedBy(value: string | null | undefined): string | null {
  if (!value) {
    return null;
  }
  let normalized = value.trim();
  if (normalized.startsWith('"') && normalized.endsWith('"')) {
    try {
      const loaded = JSON.parse(normalized) as unknown;
      if (typeof loaded === "string") {
        normalized = loaded.trim();
      }
    } catch {
      // 保留原值
    }
  }
  const upper = normalized.toUpperCase();
  return upper === "AI" || upper === "USER" ? upper : null;
}

// ==================== 账号对与可见性 ====================

function getAccountPairs(db: DbHandle, rootDir: string): Set<string> {
  const pairs = new Set<string>();
  const rows = db.sqlite
    .prepare<[], { platform: string; account_id: string }>("SELECT platform, account_id FROM bot_platform_accounts")
    .all();
  for (const row of rows) {
    if (row.platform && row.account_id) {
      pairs.add(`${row.platform}::${row.account_id}`);
    }
  }
  try {
    const config = parseToml(readFileSync(path.join(rootDir, "config", "bot_config.toml"), "utf8")) as {
      bot?: { platforms?: string[] };
    };
    for (const entry of config.bot?.platforms ?? []) {
      if (typeof entry !== "string") {
        continue;
      }
      const separatorIndex = entry.indexOf(":");
      const platform = (separatorIndex >= 0 ? entry.slice(0, separatorIndex) : entry).trim();
      const accountId = separatorIndex >= 0 ? entry.slice(separatorIndex + 1).trim() : "";
      if (platform && accountId) {
        pairs.add(`${platform}::${accountId}`);
      }
    }
  } catch {
    // 配置缺失/损坏按无备用账号处理
  }
  pairs.add("webui::webui");
  return pairs;
}

function isCurrentAccountSession(row: ChatSessionRow, pairs: Set<string>): boolean {
  const platform = String(row.platform ?? "").trim();
  const accountId = String(row.accountId ?? "").trim();
  return Boolean(platform && accountId && pairs.has(`${platform}::${accountId}`));
}

function getAllExpressionSessionIds(db: DbHandle): Set<string> {
  const ids = db.drizzle
    .select({ sessionId: expressions.sessionId })
    .from(expressions)
    .where(isNotNull(expressions.sessionId))
    .all()
    .map((row) => row.sessionId)
    .filter((sessionId): sessionId is string => Boolean(sessionId));
  return new Set(ids);
}

function getVisibleExpressionChatIds(db: DbHandle, rootDir: string, includeLegacy: boolean): Set<string> {
  const chatIds = getAllExpressionSessionIds(db);
  if (includeLegacy || chatIds.size === 0) {
    return chatIds;
  }
  const pairs = getAccountPairs(db, rootDir);
  const visible = new Set<string>();
  for (const row of db.drizzle.select().from(chatSessions).where(inArray(chatSessions.sessionId, [...chatIds])).all()) {
    if (isCurrentAccountSession(row, pairs)) {
      visible.add(row.sessionId);
    }
  }
  return visible;
}

// ==================== 聊天名 ====================

function chatSessionDisplayName(row: ChatSessionRow): string {
  if (row.groupId) {
    return `群聊${row.groupId}`;
  }
  if (row.userId) {
    return `用户${row.userId}的私聊`;
  }
  return row.sessionId;
}

function getChatName(db: DbHandle, chatId: string): string {
  try {
    const sessionRow = db.drizzle.select().from(chatSessions).where(eq(chatSessions.sessionId, chatId)).limit(1).get();
    if (sessionRow) {
      if (sessionRow.groupId && sessionRow.groupName) {
        return sessionRow.groupName;
      }
      if (!sessionRow.groupId && sessionRow.userNickname) {
        return sessionRow.userNickname;
      }
    }
    const latestMessage = db.drizzle
      .select({
        groupId: sql<string | null>`group_id`,
        groupName: sql<string | null>`group_name`,
        userCardname: sql<string | null>`user_cardname`,
        userNickname: sql<string | null>`user_nickname`,
        userId: sql<string | null>`user_id`,
      })
      .from(sql`mai_messages`)
      .where(sql`session_id = ${chatId}`)
      .orderBy(sql`timestamp DESC`)
      .limit(1)
      .get();
    if (latestMessage) {
      if (latestMessage.groupId) {
        return latestMessage.groupName || `群聊${latestMessage.groupId}`;
      }
      const privateName =
        latestMessage.userCardname ||
        latestMessage.userNickname ||
        (latestMessage.userId ? `用户${latestMessage.userId}` : null);
      if (privateName) {
        return `${privateName}的私聊`;
      }
    }
    if (sessionRow) {
      return chatSessionDisplayName(sessionRow);
    }
    return chatId;
  } catch {
    return chatId;
  }
}

function expressionToResponse(db: DbHandle, row: ExpressionRow) {
  const chatId = row.sessionId ?? "";
  return {
    id: row.id ?? 0,
    situation: row.situation,
    style: row.style,
    last_active_time: toEpochSeconds(row.lastActiveTime) ?? 0,
    chat_id: chatId,
    chat_name: chatId ? getChatName(db, chatId) : null,
    create_date: toEpochSeconds(row.createTime),
    checked: row.checked,
    modified_by: row.modifiedBy ? row.modifiedBy.toLowerCase() : null,
  };
}

// ==================== 路由 ====================

const listQuerySchema = z.object({
  page: z.coerce.number().int().min(1).default(1),
  page_size: z.coerce.number().int().min(1).max(100).default(20),
  search: z.string().optional(),
  chat_id: z.string().optional(),
  review_filter: z.string().default("all"),
  sort_by: z.string().default("time"),
  include_legacy: z
    .string()
    .optional()
    .transform((value) => value === "true"),
});

function localDatetimeNow(): string {
  return sqliteDatetime();
}

export function registerExpressionRoutes(app: FastifyInstance, deps: ExpressionRouteDeps): void {
  const { db, rootDir, reviewStore } = deps;

  const requireExistingChatId = (chatId: string | null | undefined): string => {
    const normalized = String(chatId ?? "").trim();
    if (!normalized) {
      throw HttpError(400, "缺少聊天流 ID");
    }
    const exists = db.drizzle
      .select({ id: chatSessions.id })
      .from(chatSessions)
      .where(eq(chatSessions.sessionId, normalized))
      .limit(1)
      .get();
    if (!exists) {
      throw HttpError(400, `聊天流不存在: ${normalized}`);
    }
    return normalized;
  };

  const requireNonEmptyChatId = (chatId: string | null | undefined): string => {
    const normalized = String(chatId ?? "").trim();
    if (!normalized) {
      throw HttpError(400, "缺少聊天流 ID");
    }
    return normalized;
  };

  const handle = async (reply: FastifyReply, work: () => unknown): Promise<FastifyReply> => {
    try {
      const result = work();
      return reply.send(result);
    } catch (error) {
      const err = error as { statusCode?: number; detail?: string };
      if (err.statusCode) {
        return reply.code(err.statusCode).send({ detail: err.detail });
      }
      return reply.code(500).send({ detail: String(error instanceof Error ? error.message : error) });
    }
  };

  const parseQuery = <T extends z.ZodTypeAny>(schema: T, url: string) => {
    const params = new URL(url.startsWith("/") ? `http://local${url}` : url).searchParams;
    const raw: Record<string, string | string[]> = {};
    for (const key of new Set([...params.keys()])) {
      const values = params.getAll(key);
      raw[key] = values.length > 1 ? values : values[0];
    }
    return schema.safeParse(raw);
  };

  // ------------------------------------------------------------------ 501（下一批/保留 Python）

  const groupsNotMigrated = async (_request: unknown, reply: FastifyReply) => {
    return reply.code(501).send({
      detail: "表达共享组依赖配置通配符展开（ChatConfigUtils），将在下一批迁移",
    });
  };
  const legacyNotMigrated = async (_request: unknown, reply: FastifyReply) => {
    return reply.code(501).send({
      detail: "旧版导入依赖 chat_manager 运行时会话解析，按迁移边界保留在 Python 内核侧",
    });
  };
  app.get("/api/webui/expression/groups", groupsNotMigrated);
  app.get("/api/webui/expression/clusters", groupsNotMigrated);
  app.get("/api/webui/expression/clusters/:cluster_id/members", groupsNotMigrated);
  app.post("/api/webui/expression/legacy-import/preview", legacyNotMigrated);
  app.post("/api/webui/expression/legacy-import/preview-file", legacyNotMigrated);
  app.post("/api/webui/expression/legacy-import/import", legacyNotMigrated);

  // ------------------------------------------------------------------ 聊天列表

  app.get("/api/webui/expression/chats", async (request, reply) => {
    const includeLegacy = new URL(request.url, "http://local").searchParams.get("include_legacy") === "true";
    return handle(reply, () => {
      const visible = getVisibleExpressionChatIds(db, rootDir, includeLegacy);
      const list: Array<Record<string, unknown>> = [];
      for (const sessionId of visible) {
        const row = db.drizzle.select().from(chatSessions).where(eq(chatSessions.sessionId, sessionId)).limit(1).get();
        const chatName = row
          ? row.groupId
            ? row.groupName || `群聊${row.groupId}`
            : row.userNickname || row.userCardname || `用户${row.userId}的私聊`
          : sessionId;
        list.push({ session_id: sessionId, chat_name: chatName, platform: row?.platform ?? null, account_id: row?.accountId ?? null, is_group: Boolean(row?.groupId) });
      }
      list.sort((a, b) => String(a.chat_name).localeCompare(String(b.chat_name)));
      return { success: true, data: list };
    });
  });

  app.get("/api/webui/expression/chat-targets", async (request, reply) => {
    const includeLegacy = new URL(request.url, "http://local").searchParams.get("include_legacy") === "true";
    return handle(reply, () => {
      const pairs = getAccountPairs(db, rootDir);
      const list: Array<Record<string, unknown>> = [];
      for (const row of db.drizzle.select().from(chatSessions).all()) {
        if (!includeLegacy && !isCurrentAccountSession(row, pairs)) {
          continue;
        }
        list.push({
          session_id: row.sessionId,
          chat_name: chatSessionDisplayName(row),
          platform: row.platform,
          account_id: row.accountId,
          is_group: Boolean(row.groupId),
        });
      }
      list.sort((a, b) => String(a.chat_name).localeCompare(String(b.chat_name)));
      return { success: true, data: list };
    });
  });

  // ------------------------------------------------------------------ 列表

  const reviewFilterCondition = (reviewFilter: string): SQL | undefined => {
    if (reviewFilter === "all") {
      return undefined;
    }
    if (reviewFilter === "user_checked") {
      return and(eq(expressions.checked, true), eq(expressions.modifiedBy, "USER"));
    }
    if (reviewFilter === "unchecked") {
      return eq(expressions.checked, false);
    }
    throw HttpError(400, `不支持的表达方式筛选: ${reviewFilter}`);
  };

  app.get("/api/webui/expression/list", async (request, reply) => {
    const parsed = parseQuery(
      listQuerySchema,
      request.url,
    );
    if (!parsed.success) {
      return reply.code(422).send({ detail: parsed.error.issues[0]?.message ?? "请求不合法" });
    }
    return handle(reply, () => {
      const { page, page_size, search, chat_id, review_filter, sort_by, include_legacy } = parsed.data;
      if (sort_by !== "time") {
        throw HttpError(400, `不支持的表达方式排序: ${sort_by}`);
      }
      const chatIdsParam = new URL(request.url, "http://local").searchParams.getAll("chat_ids");

      const visible = include_legacy ? null : getVisibleExpressionChatIds(db, rootDir, false);
      const validIds = include_legacy ? getAllExpressionSessionIds(db) : null;

      const filters: SQL[] = [];
      if (search && search.trim()) {
        const pattern = `%${search.trim()}%`;
        filters.push(or(like(expressions.situation, pattern), like(expressions.style, pattern))!);
      }
      if (chat_id) {
        if (!include_legacy && visible && !visible.has(chat_id)) {
          return { success: true, total: 0, page, page_size, data: [] };
        }
        filters.push(eq(expressions.sessionId, chat_id));
      } else if (chatIdsParam.length > 0) {
        const filtered = include_legacy ? chatIdsParam : chatIdsParam.filter((id) => visible?.has(id));
        if (filtered.length === 0) {
          return { success: true, total: 0, page, page_size, data: [] };
        }
        filters.push(inArray(expressions.sessionId, filtered));
      } else if (!include_legacy) {
        if (!visible || visible.size === 0) {
          return { success: true, total: 0, page, page_size, data: [] };
        }
        filters.push(inArray(expressions.sessionId, [...visible]));
      } else {
        const condition = validIds && validIds.size > 0
          ? or(isNull(expressions.sessionId), inArray(expressions.sessionId, [...validIds]))!
          : isNull(expressions.sessionId);
        filters.push(condition);
      }
      const reviewCondition = reviewFilterCondition(review_filter);
      if (reviewCondition) {
        filters.push(reviewCondition);
      }
      const where = and(...filters);

      const rows = db.drizzle
        .select()
        .from(expressions)
        .where(where)
        .orderBy(sql`${expressions.lastActiveTime} IS NULL`, desc(expressions.lastActiveTime))
        .limit(page_size)
        .offset((page - 1) * page_size)
        .all();
      const total = Number(
        db.drizzle.select({ count: sql<number>`count(*)` }).from(expressions).where(where).get()!.count,
      );
      return {
        success: true,
        total,
        page,
        page_size,
        data: rows.map((row) => expressionToResponse(db, row)),
      };
    });
  });

  // ------------------------------------------------------------------ 详情 / 创建 / 更新 / 删除

  app.get("/api/webui/expression/:expression_id", async (request, reply) => {
    const expressionId = Number.parseInt((request.params as { expression_id: string }).expression_id, 10);
    return handle(reply, () => {
      const row = db.drizzle.select().from(expressions).where(eq(expressions.id, expressionId)).limit(1).get();
      if (!row) {
        throw HttpError(404, `未找到 ID 为 ${expressionId} 的表达方式`);
      }
      return { success: true, data: expressionToResponse(db, row) };
    });
  });

  app.post("/api/webui/expression", async (request, reply) => {
    const parsed = z
      .object({ situation: z.string(), style: z.string(), chat_id: z.string() })
      .safeParse(request.body ?? {});
    if (!parsed.success) {
      return reply.code(422).send({ detail: parsed.error.issues[0]?.message ?? "请求不合法" });
    }
    return handle(reply, () => {
      const chatId = requireExistingChatId(parsed.data.chat_id);
      const now = localDatetimeNow();
      const inserted = db.drizzle
        .insert(expressions)
        .values({
          situation: parsed.data.situation,
          style: parsed.data.style,
          contentList: "[]",
          count: 0,
          lastActiveTime: now,
          createTime: now,
          sessionId: chatId,
          checked: false,
        })
        .run();
      const row = db.drizzle.select().from(expressions).where(eq(expressions.id, Number(inserted.lastInsertRowid))).get()!;
      return { success: true, message: "表达方式创建成功", data: expressionToResponse(db, row) };
    });
  });

  app.patch("/api/webui/expression/:expression_id", async (request, reply) => {
    const expressionId = Number.parseInt((request.params as { expression_id: string }).expression_id, 10);
    const parsed = z
      .object({
        situation: z.string().optional(),
        style: z.string().optional(),
        chat_id: z.string().optional(),
      })
      .safeParse(request.body ?? {});
    if (!parsed.success) {
      return reply.code(422).send({ detail: parsed.error.issues[0]?.message ?? "请求不合法" });
    }
    return handle(reply, () => {
      const body = request.body as Record<string, unknown> | null;
      const provided = Object.keys(body ?? {});
      if (provided.length === 0) {
        throw HttpError(400, "未提供任何需要更新的字段");
      }
      const row = db.drizzle.select().from(expressions).where(eq(expressions.id, expressionId)).limit(1).get();
      if (!row) {
        throw HttpError(404, `未找到 ID 为 ${expressionId} 的表达方式`);
      }
      const updates: Record<string, unknown> = { lastActiveTime: localDatetimeNow() };
      if ("situation" in body!) {
        updates.situation = body!.situation;
      }
      if ("style" in body!) {
        updates.style = body!.style;
      }
      if ("chat_id" in body!) {
        updates.sessionId = requireExistingChatId(String(body!.chat_id));
      }
      db.drizzle
        .update(expressions)
        .set(updates as Partial<typeof expressions.$inferInsert>)
        .where(eq(expressions.id, expressionId))
        .run();
      const updated = db.drizzle.select().from(expressions).where(eq(expressions.id, expressionId)).get()!;
      return { success: true, message: `成功更新 ${provided.length} 个字段`, data: expressionToResponse(db, updated) };
    });
  });

  app.patch("/api/webui/expression/:expression_id/review-status", async (request, reply) => {
    const expressionId = Number.parseInt((request.params as { expression_id: string }).expression_id, 10);
    const parsed = z.object({ approved: z.boolean() }).safeParse(request.body ?? {});
    if (!parsed.success) {
      return reply.code(422).send({ detail: "缺少 approved" });
    }
    return handle(reply, () => {
      const row = db.drizzle.select().from(expressions).where(eq(expressions.id, expressionId)).limit(1).get();
      if (!row) {
        throw HttpError(404, `未找到 ID 为 ${expressionId} 的表达方式`);
      }
      const updates: Record<string, unknown> = {
        checked: parsed.data.approved,
        modifiedBy: parsed.data.approved ? "USER" : null,
        lastActiveTime: localDatetimeNow(),
      };
      db.drizzle
        .update(expressions)
        .set(updates as Partial<typeof expressions.$inferInsert>)
        .where(eq(expressions.id, expressionId))
        .run();
      const updated = db.drizzle.select().from(expressions).where(eq(expressions.id, expressionId)).get()!;
      const message = parsed.data.approved ? "已设为人工通过" : "已设为拒绝";
      return { success: true, message, data: expressionToResponse(db, updated) };
    });
  });

  app.delete("/api/webui/expression/:expression_id", async (request, reply) => {
    const expressionId = Number.parseInt((request.params as { expression_id: string }).expression_id, 10);
    return handle(reply, () => {
      const row = db.drizzle.select().from(expressions).where(eq(expressions.id, expressionId)).limit(1).get();
      if (!row) {
        throw HttpError(404, `未找到 ID 为 ${expressionId} 的表达方式`);
      }
      db.drizzle.delete(expressions).where(eq(expressions.id, expressionId)).run();
      return { success: true, message: `成功删除表达方式: ${row.situation}` };
    });
  });

  app.post("/api/webui/expression/batch/delete", async (request, reply) => {
    const parsed = z.object({ ids: z.array(z.number().int()) }).safeParse(request.body ?? {});
    if (!parsed.success || parsed.data.ids.length === 0) {
      return reply.code(400).send({ detail: "未提供要删除的表达方式ID" });
    }
    return handle(reply, () => {
      const found = db.drizzle
        .select({ id: expressions.id })
        .from(expressions)
        .where(inArray(expressions.id, parsed.data.ids))
        .all()
        .map((row) => row.id);
      const deletedCount = found.length;
      if (deletedCount > 0) {
        db.drizzle.delete(expressions).where(inArray(expressions.id, found)).run();
      }
      return { success: true, message: `成功删除 ${deletedCount} 个表达方式` };
    });
  });

  // ------------------------------------------------------------------ 导出 / 导入 / 清空

  const exportItemSchema = z.object({
    situation: z.string(),
    style: z.string(),
    content_list: z.string().default("[]"),
    count: z.number().default(0),
    last_active_time: z.string().nullable().default(null),
    create_time: z.string().nullable().default(null),
    checked: z.boolean().default(false),
    modified_by: z.string().nullable().default(null),
  });

  app.post("/api/webui/expression/export", async (request, reply) => {
    const parsed = z
      .object({ chat_id: z.string(), ids: z.array(z.number().int()).optional() })
      .safeParse(request.body ?? {});
    if (!parsed.success) {
      return reply.code(422).send({ detail: "请求不合法" });
    }
    return handle(reply, () => {
      const chatId = requireNonEmptyChatId(parsed.data.chat_id);
      const rows = db.drizzle
        .select()
        .from(expressions)
        .where(eq(expressions.sessionId, chatId))
        .orderBy(sql`${expressions.lastActiveTime} IS NULL`, desc(expressions.lastActiveTime))
        .all();
      let filtered = rows;
      if (parsed.data.ids && parsed.data.ids.length > 0) {
        const idSet = new Set(parsed.data.ids);
        filtered = rows.filter((row) => idSet.has(row.id!));
        if (filtered.length !== idSet.size) {
          const missingIds = [...idSet].filter((id) => !new Set(filtered.map((row) => row.id)).has(id)).sort((a, b) => a - b);
          throw HttpError(400, `部分表达方式不属于该聊天或不存在: ${JSON.stringify(missingIds)}`);
        }
      }
      return {
        success: true,
        version: 1,
        type: "maibot.expression.export",
        exported_at: localDatetimeNow(),
        source_chat_name: getChatName(db, chatId),
        count: filtered.length,
        expressions: filtered.map((row) => ({
          situation: row.situation,
          style: row.style,
          content_list: row.contentList,
          count: row.count,
          last_active_time: isoFormat(row.lastActiveTime),
          create_time: isoFormat(row.createTime),
          checked: row.checked,
          modified_by: row.modifiedBy,
        })),
      };
    });
  });

  app.post("/api/webui/expression/import", async (request, reply) => {
    const parsed = z
      .object({ chat_id: z.string(), expressions: z.array(exportItemSchema) })
      .safeParse(request.body ?? {});
    if (!parsed.success) {
      return reply.code(422).send({ detail: "请求不合法" });
    }
    return handle(reply, () => {
      const chatId = requireExistingChatId(parsed.data.chat_id);
      if (parsed.data.expressions.length === 0) {
        throw HttpError(400, "导入文件中没有表达方式");
      }
      let importedCount = 0;
      let skippedCount = 0;
      let failedCount = 0;
      const existingPairs = new Set(
        db.drizzle
          .select({ situation: expressions.situation, style: expressions.style })
          .from(expressions)
          .where(eq(expressions.sessionId, chatId))
          .all()
          .map((row) => `${row.situation}\u0000${row.style}`),
      );
      for (const item of parsed.data.expressions) {
        const situation = item.situation.trim();
        const style = item.style.trim();
        if (!situation || !style) {
          failedCount += 1;
          continue;
        }
        const key = `${situation}\u0000${style}`;
        if (existingPairs.has(key)) {
          skippedCount += 1;
          continue;
        }
        db.drizzle
          .insert(expressions)
          .values({
            situation,
            style,
            contentList: item.content_list,
            count: item.count,
            lastActiveTime: parseExportDatetime(item.last_active_time),
            createTime: parseExportDatetime(item.create_time),
            sessionId: chatId,
            checked: item.checked,
            modifiedBy: parseModifiedBy(item.modified_by),
          })
          .run();
        existingPairs.add(key);
        importedCount += 1;
      }
      return {
        success: true,
        message: `导入完成：成功 ${importedCount} 个，跳过 ${skippedCount} 个，失败 ${failedCount} 个`,
        imported_count: importedCount,
        skipped_count: skippedCount,
        failed_count: failedCount,
      };
    });
  });

  app.post("/api/webui/expression/clear", async (request, reply) => {
    const parsed = z.object({ chat_id: z.string() }).safeParse(request.body ?? {});
    if (!parsed.success) {
      return reply.code(422).send({ detail: "请求不合法" });
    }
    return handle(reply, () => {
      const chatId = requireNonEmptyChatId(parsed.data.chat_id);
      const existing = db.drizzle
        .select({ id: expressions.id })
        .from(expressions)
        .where(eq(expressions.sessionId, chatId))
        .all();
      if (existing.length > 0) {
        db.drizzle.delete(expressions).where(eq(expressions.sessionId, chatId)).run();
      }
      return { success: true, message: `成功清除 ${existing.length} 个表达方式`, deleted_count: existing.length };
    });
  });

  // ------------------------------------------------------------------ 统计与审核

  app.get("/api/webui/expression/stats/summary", async (request, reply) => {
    const include_legacy = new URL(request.url, "http://local").searchParams.get("include_legacy") === "true";
    return handle(reply, () => {
      const visible = getVisibleExpressionChatIds(db, rootDir, include_legacy);
      const visibleList = [...visible];
      const totalWhere = include_legacy
        ? visible.size > 0
          ? or(isNull(expressions.sessionId), inArray(expressions.sessionId, visibleList))!
          : isNull(expressions.sessionId)
        : visible.size > 0
          ? inArray(expressions.sessionId, visibleList)
          : sql`1 = 0`;
      const total = Number(
        db.drizzle.select({ count: sql<number>`count(*)` }).from(expressions).where(totalWhere).get()!.count,
      );

      const chatStats: Record<string, number> = {};
      for (const row of db.drizzle
        .select({ sessionId: expressions.sessionId, count: sql<number>`count(*)` })
        .from(expressions)
        .where(and(isNotNull(expressions.sessionId), visible.size > 0 ? inArray(expressions.sessionId, visibleList) : sql`1 = 0`))
        .groupBy(expressions.sessionId)
        .all()) {
        if (row.sessionId) {
          chatStats[row.sessionId] = Number(row.count);
        }
      }

      const sevenDaysAgo = sqliteDatetime(new Date(Date.now() - 7 * 24 * 3600_000));
      const recentWhere = and(
        isNotNull(expressions.createTime),
        gte(expressions.createTime, sevenDaysAgo),
        include_legacy
          ? visible.size > 0
            ? or(isNull(expressions.sessionId), inArray(expressions.sessionId, visibleList))!
            : isNull(expressions.sessionId)
          : visible.size > 0
            ? inArray(expressions.sessionId, visibleList)
            : sql`1 = 0`,
      );
      const recent = Number(
        db.drizzle.select({ count: sql<number>`count(*)` }).from(expressions).where(recentWhere).get()!.count,
      );

      return {
        success: true,
        data: {
          total,
          recent_7days: recent,
          chat_count: Object.keys(chatStats).length,
          top_chats: Object.fromEntries(Object.entries(chatStats).sort((a, b) => b[1] - a[1]).slice(0, 10)),
        },
      };
    });
  });

  app.get("/api/webui/expression/review/stats", async () => {
    const countWith = (condition?: SQL) =>
      Number(db.drizzle.select({ count: sql<number>`count(*)` }).from(expressions).where(condition).get()!.count);
    return {
      total: countWith(),
      unchecked: countWith(eq(expressions.checked, false)),
      passed: countWith(eq(expressions.checked, true)),
      ai_checked: countWith(and(eq(expressions.checked, true), eq(expressions.modifiedBy, "AI"))),
      user_checked: countWith(and(eq(expressions.checked, true), eq(expressions.modifiedBy, "USER"))),
    };
  });

  app.get("/api/webui/expression/review/list", async (request, reply) => {
    const parsed = z
      .object({
        page: z.coerce.number().int().min(1).default(1),
        page_size: z.coerce.number().int().min(1).max(100).default(20),
        filter_type: z.string().default("unchecked"),
        order: z.string().default("latest"),
        search: z.string().optional(),
        chat_id: z.string().optional(),
      })
      .safeParse(Object.fromEntries(new URL(request.url, "http://local").searchParams));
    if (!parsed.success) {
      return reply.code(422).send({ detail: "请求不合法" });
    }
    return handle(reply, () => {
      const { page, page_size, filter_type, order, search, chat_id } = parsed.data;
      const excludeIds = new URL(request.url, "http://local")
        .searchParams.getAll("exclude_ids")
        .map((value) => Number.parseInt(value, 10))
        .filter((value) => Number.isInteger(value));

      let condition: SQL | undefined;
      if (filter_type === "unchecked") {
        condition = eq(expressions.checked, false);
      } else if (filter_type === "passed") {
        condition = eq(expressions.checked, true);
      } else if (filter_type !== "all") {
        condition = sql`${expressions.id} IS NULL`;
      }
      const filters: SQL[] = [];
      if (condition) {
        filters.push(condition);
      }
      if (search && search.trim()) {
        const pattern = `%${search.trim()}%`;
        filters.push(or(like(expressions.situation, pattern), like(expressions.style, pattern))!);
      }
      if (chat_id) {
        filters.push(eq(expressions.sessionId, chat_id));
      }
      if (excludeIds.length > 0) {
        filters.push(not(inArray(expressions.id, excludeIds))!);
      }
      const where = filters.length > 0 ? and(...filters) : undefined;

      const total = Number(
        db.drizzle.select({ count: sql<number>`count(*)` }).from(expressions).where(where).get()!.count,
      );
      const offset =
        order === "random" && total > 0
          ? Math.floor(Math.random() * Math.max(total - page_size, 0) + 0)
          : (page - 1) * page_size;
      let query = db.drizzle.select().from(expressions).where(where).$dynamic();
      query =
        order === "random"
          ? query.orderBy(expressions.id)
          : query.orderBy(sql`${expressions.createTime} IS NULL`, desc(expressions.createTime));
      const rows = query.limit(page_size).offset(offset).all();
      return {
        success: true,
        total,
        page,
        page_size,
        data: rows.map((row) => expressionToResponse(db, row)),
      };
    });
  });

  app.get("/api/webui/expression/review/logs", async (request, reply) => {
    const query = new URL(request.url, "http://local").searchParams;
    return handle(reply, () => {
      const limit = Math.min(Math.max(Number.parseInt(query.get("limit") ?? "50", 10) || 50, 1), 200);
      const passedParam = query.get("passed");
      const chatId = query.get("chat_id");
      const entries = reviewStore.getRecentAiReviewLogs({
        limit,
        passed: passedParam === null ? null : passedParam === "true",
        sessionId: chatId,
      });
      return {
        success: true,
        total: entries.length,
        data: entries.map((entry) => ({
          id: String(entry.id ?? ""),
          created_at: toEpochSeconds(String(entry.created_at ?? "").replace(" ", "T")) ?? 0,
          expression_id: typeof entry.expression_id === "number" ? entry.expression_id : null,
          session_id: String(entry.session_id ?? ""),
          chat_name: entry.session_id ? getChatName(db, String(entry.session_id)) : null,
          passed: Boolean(entry.passed),
          reason: String(entry.reason ?? ""),
          situation: String(entry.situation ?? ""),
          style: String(entry.style ?? ""),
          source: String(entry.source ?? ""),
          error: entry.error ? String(entry.error) : null,
          rescued: Boolean(entry.rescued),
          rescued_expression_id: (entry.rescued_expression_id as number | null) ?? null,
          rescued_at: entry.rescued_at ? toEpochSeconds(String(entry.rescued_at).replace(" ", "T")) : null,
        })),
      };
    });
  });

  app.post("/api/webui/expression/review/logs/:review_log_id/approve", async (request, reply) => {
    const reviewLogId = (request.params as { review_log_id: string }).review_log_id;
    return handle(reply, () => {
      const reviewLog = reviewStore.getAiReviewLog(reviewLogId);
      if (!reviewLog) {
        throw HttpError(404, `未找到审核日志: ${reviewLogId}`);
      }
      const sessionId = requireNonEmptyChatId(String(reviewLog.session_id ?? ""));
      const situation = String(reviewLog.situation ?? "").trim();
      const style = String(reviewLog.style ?? "").trim();
      if (!situation || !style) {
        throw HttpError(400, "审核日志缺少表达方式内容，无法恢复");
      }
      const now = localDatetimeNow();
      const expressionId = typeof reviewLog.expression_id === "number" ? reviewLog.expression_id : null;
      let row = expressionId
        ? db.drizzle.select().from(expressions).where(eq(expressions.id, expressionId)).limit(1).get()
        : undefined;
      let created = false;
      if (!row) {
        row = db.drizzle
          .select()
          .from(expressions)
          .where(
            and(
              eq(expressions.sessionId, sessionId),
              eq(expressions.situation, situation),
              eq(expressions.style, style),
            ),
          )
          .limit(1)
          .get();
      }
      if (!row) {
        const inserted = db.drizzle
          .insert(expressions)
          .values({
            situation,
            style,
            contentList: JSON.stringify([situation]),
            count: 1,
            lastActiveTime: now,
            createTime: now,
            sessionId,
            checked: true,
            modifiedBy: "USER",
          })
          .run();
        row = db.drizzle.select().from(expressions).where(eq(expressions.id, Number(inserted.lastInsertRowid))).get()!;
        created = true;
      } else {
        db.drizzle
          .update(expressions)
          .set({ checked: true, modifiedBy: "USER", lastActiveTime: now })
          .where(eq(expressions.id, row.id!))
          .run();
        row = db.drizzle.select().from(expressions).where(eq(expressions.id, row.id!)).get()!;
      }
      const restoredId = row.id;
      if (restoredId === null || restoredId === undefined) {
        throw HttpError(500, "表达方式恢复后缺少 ID");
      }
      reviewStore.appendManualRescueLog(reviewLogId, restoredId);
      const message = created ? "已从 AI 审核日志救回表达方式并设为人工通过" : "已设为人工审核通过";
      return { success: true, message, data: expressionToResponse(db, row) };
    });
  });

  app.post("/api/webui/expression/review/batch", async (request, reply) => {
    const parsed = z
      .object({
        items: z.array(z.object({ id: z.number().int(), approved: z.boolean(), require_unchecked: z.boolean().default(true) })),
      })
      .safeParse(request.body ?? {});
    if (!parsed.success) {
      return reply.code(422).send({ detail: "请求不合法" });
    }
    return handle(reply, () => {
      if (parsed.data.items.length === 0) {
        throw HttpError(400, "未提供要审核的表达方式");
      }
      const results: Array<Record<string, unknown>> = [];
      let succeeded = 0;
      let failed = 0;
      for (const item of parsed.data.items) {
        const row = db.drizzle.select().from(expressions).where(eq(expressions.id, item.id)).limit(1).get();
        if (!row) {
          results.push({ id: item.id, success: false, message: `未找到 ID 为 ${item.id} 的表达方式` });
          failed += 1;
          continue;
        }
        if (!item.approved) {
          db.drizzle.delete(expressions).where(eq(expressions.id, item.id)).run();
        } else {
          db.drizzle
            .update(expressions)
            .set({ checked: true, modifiedBy: "USER", lastActiveTime: localDatetimeNow() })
            .where(eq(expressions.id, item.id))
            .run();
        }
        results.push({ id: item.id, success: true, message: item.approved ? "通过" : "拒绝并删除" });
        succeeded += 1;
      }
      return { success: true, total: parsed.data.items.length, succeeded, failed, results };
    });
  });
}

function HttpError(statusCode: number, detail: string): Error & { statusCode: number; detail: string } {
  return Object.assign(new Error(detail), { statusCode, detail });
}
