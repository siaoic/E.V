/**
 * 黑话管理路由（对照 src/webui/routers/jargon.py 逐端点迁移）。
 *
 * 关键语义：
 * - `session_id_dict` 是 JSON 计数字典（{"session_id": count}），主聊天 = 计数最高者，
 *   遍历顺序 = JSON 文档顺序（JSON.parse 保持插入序）；
 * - 有效黑话判定 = is_jargon 为真 且 含义非空白（`effective_jargon_condition`）；
 * - 聊天显示名：群聊 → 群名/`群聊{id}`；私聊 → `xx的私聊`；未知 → session_id 前 20 字。
 *   列表走批量缓存（chat_sessions 优先，messages 兜底）；详情/创建走消息优先路径；
 * - 资源归属校验：目标聊天流必须存在于 `chat_sessions` 表（对应
 *   `chat_manager.get_existing_session_by_session_id` 的 DB 回落路径）。
 */

import { and, desc, eq, inArray, isNull, like, not, or, sql, type SQL } from "drizzle-orm";
import { z } from "zod";
import type { FastifyInstance } from "fastify";

import type { DbHandle } from "../../db/client.js";
import { chatSessions, jargons, maiMessages as messages } from "../../db/schema.js";

type JargonsRow = typeof jargons.$inferSelect;
type ChatSessionRow = typeof chatSessions.$inferSelect;

const CREATED_BY_AI = "AI";
const CREATED_BY_MANUAL = "MANUAL";

// ==================== session_id_dict 工具 ====================

function parseSessionIdDict(raw: string | null | undefined): Array<[string, number]> {
  if (!raw) {
    return [];
  }
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return [];
  }
  if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
    return [];
  }
  const entries: Array<[string, number]> = [];
  for (const [sessionId, count] of Object.entries(parsed as Record<string, unknown>)) {
    if (typeof sessionId !== "string") {
      continue;
    }
    if (typeof count === "number") {
      entries.push([sessionId, count]);
    } else {
      const parsedCount = Number.parseInt(String(count), 10);
      entries.push([sessionId, Number.isNaN(parsedCount) ? 0 : parsedCount]);
    }
  }
  return entries;
}

function getSessionIds(raw: string | null | undefined): string[] {
  return parseSessionIdDict(raw).map(([sessionId]) => sessionId);
}

function dumpSessionIdDict(counts: Record<string, number>): string {
  return JSON.stringify(counts);
}

function buildSessionIdDictForSession(sessionId: string, count = 1): string {
  return dumpSessionIdDict({ [sessionId]: count });
}

function buildSessionIdDictForSessions(sessionIds: string[], count = 1): string {
  return dumpSessionIdDict(Object.fromEntries(sessionIds.map((sessionId) => [sessionId, count])));
}

/** JSON 转义（ensure_ascii=True 风格）+ 常规形式，两种文本标记都参与 SQL 过滤。 */
function sessionIdDictSearchTokens(sessionId: string): string[] {
  const normalized = sessionId.trim();
  if (!normalized) {
    return [];
  }
  const plain = JSON.stringify(normalized);
  const escaped = plain.replace(/[\u0080-\uFFFF]/g, (ch) => {
    const code = ch.charCodeAt(0).toString(16).padStart(4, "0");
    return `\\u${code}`;
  });
  return plain === escaped ? [plain] : [plain, escaped];
}

function sessionIdDictFilter(sessionIds: string[]): SQL | undefined {
  const conditions: SQL[] = [];
  const seen = new Set<string>();
  for (const sessionId of sessionIds) {
    for (const token of sessionIdDictSearchTokens(sessionId)) {
      if (seen.has(token)) {
        continue;
      }
      seen.add(token);
      conditions.push(sql`instr(${jargons.sessionIdDict}, ${token}) > 0`);
    }
  }
  if (conditions.length === 0) {
    return undefined;
  }
  return or(...conditions);
}

const hasMeaningCondition = sql`length(trim(${jargons.meaning})) > 0`;
const effectiveJargonCondition = and(eq(jargons.isJargon, 1), hasMeaningCondition)!;
const noJargonCondition = or(
  eq(jargons.isJargon, 0),
  isNull(jargons.isJargon),
  not(effectiveJargonCondition),
)!;

function isLegacyEmptyMeaningJargon(isJargon: number | null, meaning: string | null): boolean {
  return Boolean(isJargon) && !String(meaning ?? "").trim();
}

function normalizeCreatedBy(createdBy: string | null): string {
  if (createdBy === CREATED_BY_MANUAL) {
    return CREATED_BY_MANUAL;
  }
  return CREATED_BY_AI; // 未知/历史空值一律按 AI 展示（原实现如此）
}

function normalizeSessionIdCandidates(raw: string): string[] {
  if (!raw) {
    return [];
  }
  try {
    const parsed = JSON.parse(raw);
    if (Array.isArray(parsed)) {
      // 兼容旧格式：[["session_id", user_id], ...]
      return parsed
        .filter((item): item is unknown[] => Array.isArray(item) && item.length >= 1)
        .map((item) => String(item[0]));
    }
    return [raw];
  } catch {
    return [raw];
  }
}

// ==================== 聊天显示名 ====================

function chatSessionDisplayName(row: ChatSessionRow): string {
  if (row.groupId) {
    return row.groupName || `群聊${row.groupId}`;
  }
  if (row.userId) {
    const privateName = row.userCardname || row.userNickname || `用户${row.userId}`;
    return `${privateName}的私聊`;
  }
  return row.sessionId.slice(0, 20);
}

function messageDisplayName(row: {
  groupId: string | null;
  groupName: string | null;
  userCardname: string | null;
  userNickname: string | null;
  userId: string | null;
}): string | null {
  if (row.groupId) {
    return row.groupName || `群聊${row.groupId}`;
  }
  const privateName = row.userCardname || row.userNickname || (row.userId ? `用户${row.userId}` : null);
  return privateName ? `${privateName}的私聊` : null;
}

function buildDisplayNameCache(
  db: DbHandle,
  sessionIds: string[],
  includeMessageFallback: boolean,
): Map<string, string> {
  const unique = [...new Set(sessionIds.filter((sessionId) => sessionId))];
  const cache = new Map<string, string>();
  if (unique.length === 0) {
    return cache;
  }
  const chatRows = db.drizzle
    .select()
    .from(chatSessions)
    .where(inArray(chatSessions.sessionId, unique))
    .all();
  for (const row of chatRows) {
    cache.set(row.sessionId, chatSessionDisplayName(row));
  }
  if (includeMessageFallback) {
    for (const sessionId of unique) {
      if (cache.has(sessionId)) {
        continue;
      }
      const latest = db.drizzle
        .select({
          groupId: messages.groupId,
          groupName: messages.groupName,
          userCardname: messages.userCardname,
          userNickname: messages.userNickname,
          userId: messages.userId,
        })
        .from(messages)
        .where(eq(messages.sessionId, sessionId))
        .orderBy(desc(messages.timestamp))
        .limit(1)
        .get();
      const displayName = latest ? messageDisplayName(latest) : null;
      if (displayName) {
        cache.set(sessionId, displayName);
      }
    }
  }
  for (const sessionId of unique) {
    if (!cache.has(sessionId)) {
      cache.set(sessionId, sessionId.slice(0, 20));
    }
  }
  return cache;
}

/** 详情/创建路径：最新消息名优先，其次 chat_sessions，最后截断 id。 */
function getDisplayNameForSessionId(db: DbHandle, sessionIdStr: string): string {
  const candidates = normalizeSessionIdCandidates(sessionIdStr);
  if (candidates.length === 0) {
    return sessionIdStr.slice(0, 20);
  }
  const primary = candidates[0];
  const latest = db.drizzle
    .select({
      groupId: messages.groupId,
      groupName: messages.groupName,
      userCardname: messages.userCardname,
      userNickname: messages.userNickname,
      userId: messages.userId,
    })
    .from(messages)
    .where(eq(messages.sessionId, primary))
    .orderBy(desc(messages.timestamp))
    .limit(1)
    .get();
  if (latest) {
    const displayName = messageDisplayName(latest);
    if (displayName) {
      return displayName;
    }
  }
  const chatSession = db.drizzle
    .select()
    .from(chatSessions)
    .where(eq(chatSessions.sessionId, primary))
    .limit(1)
    .get();
  if (!chatSession) {
    return primary.slice(0, 20);
  }
  return chatSessionDisplayName(chatSession);
}

// ==================== 行转换 ====================

function jargonRowToDict(
  row: JargonsRow,
  db: DbHandle,
  cache?: Map<string, string>,
): Record<string, unknown> {
  const sessionIds = getSessionIds(row.sessionIdDict);
  const resolve = (sessionId: string) => cache?.get(sessionId) ?? getDisplayNameForSessionId(db, sessionId);
  const chatNames = sessionIds.map(resolve);
  const legacyEmpty = isLegacyEmptyMeaningJargon(row.isJargon, row.meaning);
  return {
    id: row.id,
    content: row.content,
    meaning: row.meaning,
    session_id: sessionIds[0] ?? "",
    session_ids: sessionIds,
    chat_name: chatNames[0] ?? null,
    chat_names: chatNames,
    count: row.count,
    is_jargon: Boolean(row.isJargon) && !legacyEmpty,
    is_legacy_empty_meaning: legacyEmpty,
    is_complete: row.isComplete,
    is_global: row.isGlobal,
    created_by: normalizeCreatedBy(row.createdBy),
    created_timestamp: row.createdTimestamp ?? "",
    updated_timestamp: row.updatedTimestamp ?? "",
  };
}

// ==================== 校验 ====================

async function requireExistingSessionIds(
  db: DbHandle,
  sessionIds: string[] | undefined,
): Promise<string[]> {
  const normalized: string[] = [];
  const seen = new Set<string>();
  for (const raw of sessionIds ?? []) {
    const sessionId = String(raw ?? "").trim();
    if (!sessionId) {
      throw HttpError(400, "缺少聊天流 ID");
    }
    const exists = db.drizzle
      .select({ id: chatSessions.id })
      .from(chatSessions)
      .where(eq(chatSessions.sessionId, sessionId))
      .limit(1)
      .get();
    if (!exists) {
      throw HttpError(400, `聊天流不存在: ${sessionId}`);
    }
    if (!seen.has(sessionId)) {
      seen.add(sessionId);
      normalized.push(sessionId);
    }
  }
  if (normalized.length === 0) {
    throw HttpError(400, "缺少聊天流 ID");
  }
  return normalized;
}

function HttpError(statusCode: number, detail: string): Error & { statusCode: number; detail: string } {
  return Object.assign(new Error(detail), { statusCode, detail });
}

function scopesOverlap(row: JargonsRow, targetSessionIds: Set<string>, targetIsGlobal: boolean): boolean {
  if (targetIsGlobal || row.isGlobal) {
    return true;
  }
  const own = new Set(parseSessionIdDict(row.sessionIdDict).map(([sessionId]) => sessionId));
  for (const sessionId of targetSessionIds) {
    if (own.has(sessionId)) {
      return true;
    }
  }
  return false;
}

function localDatetimeNow(): string {
  return new Date().toLocaleString("sv-SE").replace("T", " ");
}

// ==================== 路由 ====================

const listQuerySchema = z.object({
  page: z.coerce.number().int().min(1).default(1),
  page_size: z.coerce.number().int().min(1).max(100).default(20),
  search: z.string().optional(),
  session_id: z.string().optional(),
  jargon_status: z.enum(["confirmed_jargon", "confirmed_not_jargon", "manual_jargon", "pending"]).optional(),
  is_jargon: z
    .string()
    .optional()
    .transform((value) => (value === undefined ? undefined : value === "true")),
  is_complete: z
    .string()
    .optional()
    .transform((value) => (value === undefined ? undefined : value === "true")),
  is_global: z
    .string()
    .optional()
    .transform((value) => (value === undefined ? undefined : value === "true")),
});

const exportItemSchema = z.object({
  content: z.string(),
  meaning: z.string().default(""),
  count: z.number().default(0),
  is_jargon: z.boolean().default(false),
  is_complete: z.boolean().default(false),
  is_global: z.boolean().default(false),
  created_by: z.string().default(CREATED_BY_MANUAL),
  targets: z
    .array(
      z.object({
        platform: z.string(),
        id: z.string(),
        type: z.enum(["group", "private"]),
        account_id: z.string().nullable().optional(),
        scope: z.string().nullable().optional(),
        count: z.number().default(0),
      }),
    )
    .nullable()
    .optional(),
});

export function registerJargonRoutes(app: FastifyInstance, db: DbHandle): void {
  const { drizzle } = db;

  app.get("/api/webui/jargon/list", async (request, reply) => {
    const parsed = listQuerySchema.safeParse(request.query);
    if (!parsed.success) {
      return reply.code(422).send({ detail: parsed.error.issues[0]?.message ?? "请求不合法" });
    }
    const { page, page_size, search, session_id, jargon_status, is_jargon, is_complete, is_global } = parsed.data;

    const filters: SQL[] = [];
    if (search && search.trim()) {
      filters.push(like(jargons.content, `%${search.trim()}%`));
    }
    if (session_id) {
      const candidates = normalizeSessionIdCandidates(session_id);
      const dictFilter = sessionIdDictFilter(candidates.length > 0 ? candidates : [session_id]);
      if (dictFilter !== undefined) {
        filters.push(dictFilter);
      }
    }
    if (jargon_status === "confirmed_jargon") {
      filters.push(effectiveJargonCondition);
    } else if (jargon_status === "confirmed_not_jargon" || jargon_status === "pending") {
      filters.push(noJargonCondition);
    } else if (jargon_status === "manual_jargon") {
      filters.push(eq(jargons.createdBy, CREATED_BY_MANUAL));
    } else if (is_jargon !== undefined) {
      filters.push(is_jargon ? effectiveJargonCondition : noJargonCondition);
    }
    if (is_complete !== undefined) {
      filters.push(eq(jargons.isComplete, is_complete));
    }
    if (is_global !== undefined) {
      filters.push(eq(jargons.isGlobal, is_global));
    }
    const where = filters.length > 0 ? and(...filters) : undefined;

    const rows = drizzle
      .select()
      .from(jargons)
      .where(where)
      .orderBy(desc(jargons.count), desc(jargons.id))
      .limit(page_size)
      .offset((page - 1) * page_size)
      .all();
    const total = Number(
      drizzle.select({ count: sql<number>`count(*)` }).from(jargons).where(where).get()!.count,
    );

    const pageSessionIds = rows.flatMap((row) => getSessionIds(row.sessionIdDict));
    const cache = buildDisplayNameCache(db, pageSessionIds, true);
    return {
      success: true,
      total,
      page,
      page_size,
      data: rows.map((row) => jargonRowToDict(row, db, cache)),
    };
  });

  app.get("/api/webui/jargon/chats", async (request) => {
    const includeEmpty = (request.query as { include_empty?: string }).include_empty === "true";
    const seen = new Set<string>();
    if (includeEmpty) {
      for (const row of drizzle.select({ dict: jargons.sessionIdDict }).from(jargons).all()) {
        for (const sessionId of getSessionIds(row.dict)) {
          seen.add(sessionId);
        }
      }
    } else {
      for (const row of drizzle
        .select({ dict: jargons.sessionIdDict })
        .from(jargons)
        .where(eq(jargons.isGlobal, false))
        .all()) {
        for (const sessionId of getSessionIds(row.dict)) {
          seen.add(sessionId);
        }
      }
    }

    const chatRows = includeEmpty
      ? drizzle.select().from(chatSessions).all()
      : seen.size > 0
        ? drizzle.select().from(chatSessions).where(inArray(chatSessions.sessionId, [...seen])).all()
        : [];
    const cache = new Map<string, string>();
    for (const row of chatRows) {
      cache.set(row.sessionId, chatSessionDisplayName(row));
    }
    const orphans = [...seen].filter((sessionId) => !cache.has(sessionId));
    for (const [sessionId, name] of buildDisplayNameCache(db, orphans, true)) {
      cache.set(sessionId, name);
    }

    const bySessionId = new Map<string, Record<string, unknown>>();
    for (const row of chatRows) {
      bySessionId.set(row.sessionId, {
        session_id: row.sessionId,
        chat_name: cache.get(row.sessionId) ?? row.sessionId.slice(0, 20),
        platform: row.platform,
        account_id: row.accountId,
        is_group: Boolean(row.groupId),
      });
    }
    // 兼容旧数据：黑话里可能残留已不存在的聊天流
    for (const sessionId of seen) {
      if (bySessionId.has(sessionId)) {
        continue;
      }
      bySessionId.set(sessionId, {
        session_id: sessionId,
        chat_name: cache.get(sessionId) ?? sessionId.slice(0, 20),
        platform: null,
        account_id: null,
        is_group: false,
      });
    }

    const result = [...bySessionId.values()].sort((a, b) => {
      const byName = String(a.chat_name).localeCompare(String(b.chat_name));
      return byName || String(a.session_id).localeCompare(String(b.session_id));
    });
    return { success: true, data: result };
  });

  app.get("/api/webui/jargon/stats/summary", async () => {
    const countWith = (condition?: SQL) =>
      Number(
        drizzle
          .select({ count: sql<number>`count(*)` })
          .from(jargons)
          .where(condition)
          .get()!.count,
      );

    const topChats = new Map<string, number>();
    for (const row of drizzle.select({ dict: jargons.sessionIdDict }).from(jargons).all()) {
      for (const [sessionId] of parseSessionIdDict(row.dict)) {
        topChats.set(sessionId, (topChats.get(sessionId) ?? 0) + 1);
      }
    }
    const topChatsDict = Object.fromEntries(
      [...topChats.entries()].sort((a, b) => b[1] - a[1]).slice(0, 5),
    );

    return {
      success: true,
      data: {
        total: countWith(),
        confirmed_jargon: countWith(effectiveJargonCondition),
        confirmed_not_jargon: countWith(noJargonCondition),
        pending: countWith(sql`${jargons.isJargon} IS NULL`),
        manual_jargon: countWith(eq(jargons.createdBy, CREATED_BY_MANUAL)),
        global_count: countWith(eq(jargons.isGlobal, true)),
        complete_count: countWith(eq(jargons.isComplete, true)),
        chat_count: topChats.size,
        top_chats: topChatsDict,
      },
    };
  });

  app.post("/api/webui/jargon/export", async (request, reply) => {
    const parsed = z
      .object({ ids: z.array(z.number().int()).optional(), include_chat_info: z.boolean().default(false) })
      .safeParse(request.body ?? {});
    if (!parsed.success) {
      return reply.code(422).send({ detail: parsed.error.issues[0]?.message ?? "请求不合法" });
    }
    const { ids, include_chat_info } = parsed.data;

    let rows = drizzle.select().from(jargons).orderBy(desc(jargons.count), desc(jargons.id)).all();
    if (ids && ids.length > 0) {
      const uniqueIds = [...new Set(ids)];
      rows = rows.filter((row) => uniqueIds.includes(row.id!));
      if (rows.length !== uniqueIds.length) {
        const foundIds = new Set(rows.map((row) => row.id));
        const missingIds = [...new Set(ids)].filter((id) => !foundIds.has(id)).sort((a, b) => a - b);
        return reply.code(400).send({ detail: `部分黑话不存在: ${JSON.stringify(missingIds)}` });
      }
    }

    let chatBySessionId: Map<string, ChatSessionRow> | null = null;
    if (include_chat_info) {
      const sessionIds = [...new Set(rows.flatMap((row) => getSessionIds(row.sessionIdDict)))];
      chatBySessionId = new Map();
      if (sessionIds.length > 0) {
        for (const row of drizzle.select().from(chatSessions).where(inArray(chatSessions.sessionId, sessionIds)).all()) {
          chatBySessionId.set(row.sessionId, row);
        }
      }
    }

    const items = rows.map((row) => {
      const sessionCounts = parseSessionIdDict(row.sessionIdDict);
      let targets: Array<Record<string, unknown>> | null = null;
      if (chatBySessionId !== null) {
        targets = [];
        for (const [sessionId, count] of sessionCounts) {
          const chatSession = chatBySessionId.get(sessionId);
          if (!chatSession) {
            continue;
          }
          if (chatSession.groupId) {
            targets.push({
              platform: chatSession.platform,
              id: chatSession.groupId,
              type: "group",
              account_id: chatSession.accountId,
              scope: chatSession.scope,
              count,
            });
          } else if (chatSession.userId) {
            targets.push({
              platform: chatSession.platform,
              id: chatSession.userId,
              type: "private",
              account_id: chatSession.accountId,
              scope: chatSession.scope,
              count,
            });
          }
        }
      }
      return {
        content: row.content,
        meaning: row.meaning ?? "",
        count: row.count,
        is_jargon: Boolean(row.isJargon),
        is_complete: row.isComplete,
        is_global: row.isGlobal,
        created_by: normalizeCreatedBy(row.createdBy),
        targets,
      };
    });

    return {
      success: true,
      version: 1,
      type: "maibot.jargon.export",
      exported_at: new Date().toLocaleString("sv-SE").replace("T", " "),
      include_chat_info,
      count: items.length,
      jargons: items,
    };
  });

  app.post("/api/webui/jargon/import", async (request, reply) => {
    const parsed = z
      .object({
        target_session_ids: z.array(z.string()),
        jargons: z.array(exportItemSchema),
        conflict_strategy: z.enum(["skip", "overwrite"]).default("skip"),
      })
      .safeParse(request.body ?? {});
    if (!parsed.success) {
      return reply.code(422).send({ detail: parsed.error.issues[0]?.message ?? "请求不合法" });
    }
    let targetSessionIds: string[];
    try {
      targetSessionIds = await requireExistingSessionIds(db, parsed.data.target_session_ids);
    } catch (error) {
      const err = error as { statusCode?: number; detail?: string };
      return reply.code(err.statusCode ?? 400).send({ detail: err.detail ?? "请求不合法" });
    }
    if (parsed.data.jargons.length === 0) {
      return reply.code(400).send({ detail: "导入文件中没有黑话" });
    }

    let importedCount = 0;
    let skippedCount = 0;
    let failedCount = 0;
    const targetSet = new Set(targetSessionIds);
    const now = localDatetimeNow();

    for (const item of parsed.data.jargons) {
      const content = item.content.trim();
      if (!content) {
        failedCount += 1;
        continue;
      }
      const meaning = item.meaning.trim();
      const sameContent = drizzle.select().from(jargons).where(eq(jargons.content, content)).all();
      const overlapping = sameContent.filter((row) => scopesOverlap(row, targetSet, false));

      if (overlapping.length > 0 && parsed.data.conflict_strategy === "skip") {
        skippedCount += 1;
        continue;
      }
      if (parsed.data.conflict_strategy === "overwrite") {
        for (const row of overlapping) {
          drizzle.delete(jargons).where(eq(jargons.id, row.id!)).run();
        }
      }
      const sessionCounts = Object.fromEntries(targetSessionIds.map((sessionId) => [sessionId, Math.max(item.count, 1)]));
      drizzle
        .insert(jargons)
        .values({
          content,
          meaning,
          sessionIdDict: dumpSessionIdDict(sessionCounts),
          count: Math.max(item.count, 0),
          isJargon: item.is_jargon && meaning !== "" ? 1 : 0,
          isComplete: item.is_complete,
          isGlobal: false,
          createdBy: CREATED_BY_MANUAL,
          createdTimestamp: now,
          updatedTimestamp: now,
          lastInferenceCount: 0,
        })
        .run();
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

  app.get("/api/webui/jargon/:jargon_id", async (request, reply) => {
    const jargonId = Number.parseInt((request.params as { jargon_id: string }).jargon_id, 10);
    const row = drizzle.select().from(jargons).where(eq(jargons.id, jargonId)).limit(1).get();
    if (!row) {
      return reply.code(404).send({ detail: "黑话不存在" });
    }
    return { success: true, data: jargonRowToDict(row, db) };
  });

  app.post("/api/webui/jargon", async (request, reply) => {
    const parsed = z
      .object({
        content: z.string(),
        meaning: z.string().nullable().optional(),
        session_id: z.string().nullable().optional(),
        session_ids: z.array(z.string()).nullable().optional(),
        is_global: z.boolean().default(false),
      })
      .safeParse(request.body ?? {});
    if (!parsed.success) {
      return reply.code(422).send({ detail: parsed.error.issues[0]?.message ?? "请求不合法" });
    }
    const content = parsed.data.content.trim();
    if (!content) {
      return reply.code(400).send({ detail: "黑话内容不能为空" });
    }
    const meaning = (parsed.data.meaning ?? "").trim();

    const rawSessionIds = parsed.data.session_ids ?? [parsed.data.session_id];
    let sessionIds: string[];
    try {
      sessionIds = await requireExistingSessionIds(db, (rawSessionIds ?? []).map((value) => String(value ?? "")));
    } catch (error) {
      const err = error as { statusCode?: number; detail?: string };
      return reply.code(err.statusCode ?? 400).send({ detail: err.detail ?? "请求不合法" });
    }
    const targetSet = new Set(sessionIds);

    const sameContent = drizzle.select().from(jargons).where(eq(jargons.content, content)).all();
    const existingManual = sameContent.find(
      (row) => normalizeCreatedBy(row.createdBy) === CREATED_BY_MANUAL && scopesOverlap(row, targetSet, parsed.data.is_global),
    );
    if (existingManual) {
      return reply.code(400).send({ detail: "该范围中已存在相同内容的手动黑话" });
    }
    for (const row of sameContent) {
      if (normalizeCreatedBy(row.createdBy) !== CREATED_BY_AI) {
        continue;
      }
      if (!scopesOverlap(row, targetSet, parsed.data.is_global)) {
        continue;
      }
      drizzle.delete(jargons).where(eq(jargons.id, row.id!)).run();
    }

    const now = localDatetimeNow();
    const inserted = drizzle
      .insert(jargons)
      .values({
        content,
        meaning,
        sessionIdDict: buildSessionIdDictForSessions(sessionIds),
        count: 0,
        isJargon: meaning ? 1 : 0,
        isComplete: false,
        isGlobal: parsed.data.is_global,
        createdBy: CREATED_BY_MANUAL,
        createdTimestamp: now,
        updatedTimestamp: now,
        lastInferenceCount: 0,
      })
      .run();

    const newId = Number(inserted.lastInsertRowid);
    const row = drizzle.select().from(jargons).where(eq(jargons.id, newId)).get()!;
    const sessionIdsFromDict = getSessionIds(row.sessionIdDict);
    const chatNames = sessionIdsFromDict.map((sessionId) => getDisplayNameForSessionId(db, sessionId));
    return {
      success: true,
      message: "创建成功",
      data: {
        id: row.id,
        content: row.content,
        meaning: row.meaning,
        session_id: sessionIdsFromDict[0] ?? "",
        session_ids: sessionIdsFromDict,
        chat_name: chatNames[0] ?? null,
        chat_names: chatNames,
        count: row.count,
        is_jargon: Boolean(row.isJargon),
        is_legacy_empty_meaning: false,
        is_complete: row.isComplete,
        is_global: row.isGlobal,
        created_by: CREATED_BY_MANUAL,
        created_timestamp: row.createdTimestamp,
        updated_timestamp: row.updatedTimestamp,
      },
    };
  });

  app.patch("/api/webui/jargon/:jargon_id", async (request, reply) => {
    const jargonId = Number.parseInt((request.params as { jargon_id: string }).jargon_id, 10);
    const parsed = z
      .object({
        content: z.string().nullable().optional(),
        meaning: z.string().nullable().optional(),
        session_id: z.string().nullable().optional(),
        session_ids: z.array(z.string()).nullable().optional(),
        is_global: z.boolean().nullable().optional(),
        is_jargon: z.boolean().nullable().optional(),
        created_by: z.string().nullable().optional(),
      })
      .safeParse(request.body ?? {});
    if (!parsed.success) {
      return reply.code(422).send({ detail: parsed.error.issues[0]?.message ?? "请求不合法" });
    }
    const body = request.body as Record<string, unknown> | null;
    const updateData = parsed.data;
    const hasAny = body !== null && Object.keys(body).length > 0;
    if (!hasAny) {
      // 原实现：空更新返回当前数据
    }

    const row = drizzle.select().from(jargons).where(eq(jargons.id, jargonId)).limit(1).get();
    if (!row) {
      return reply.code(404).send({ detail: "黑话不存在" });
    }

    const updates: Record<string, unknown> = {};
    try {
      if ("session_ids" in updateData && updateData.session_ids !== null && updateData.session_ids !== undefined) {
        const sessionIds = await requireExistingSessionIds(db, updateData.session_ids);
        updates.sessionIdDict = buildSessionIdDictForSessions(sessionIds, Math.max(row.count, 1));
      } else if ("session_id" in updateData && updateData.session_id !== null && updateData.session_id !== undefined) {
        const [sessionId] = await requireExistingSessionIds(db, [updateData.session_id]);
        updates.sessionIdDict = buildSessionIdDictForSession(sessionId, Math.max(row.count, 1));
      }
      if ("content" in updateData && updateData.content !== null && updateData.content !== undefined) {
        const content = updateData.content.trim();
        if (!content) {
          return reply.code(400).send({ detail: "黑话内容不能为空" });
        }
        updates.content = content;
      }
      if ("meaning" in updateData) {
        updates.meaning = updateData.meaning ?? "";
      }
      if ("is_global" in updateData && updateData.is_global !== null && updateData.is_global !== undefined) {
        updates.isGlobal = updateData.is_global;
      }
      if ("is_jargon" in updateData && updateData.is_jargon !== null && updateData.is_jargon !== undefined) {
        updates.isJargon = updateData.is_jargon ? 1 : 0;
      }
      if ("created_by" in updateData && updateData.created_by !== null && updateData.created_by !== undefined) {
        updates.createdBy = updateData.created_by;
      }
      if (hasAny) {
        updates.updatedTimestamp = localDatetimeNow();
      }
      if (Object.keys(updates).length > 0) {
        drizzle
          .update(jargons)
          .set(updates as Partial<typeof jargons.$inferInsert>)
          .where(eq(jargons.id, jargonId))
          .run();
      }
    } catch (error) {
      const err = error as { statusCode?: number; detail?: string };
      if (err.statusCode) {
        return reply.code(err.statusCode).send({ detail: err.detail ?? "请求不合法" });
      }
      throw error;
    }

    const updated = drizzle.select().from(jargons).where(eq(jargons.id, jargonId)).limit(1).get()!;
    void updateData;
    return { success: true, message: "更新成功", data: jargonRowToDict(updated, db) };
  });

  app.delete("/api/webui/jargon/:jargon_id", async (request, reply) => {
    const jargonId = Number.parseInt((request.params as { jargon_id: string }).jargon_id, 10);
    const row = drizzle.select().from(jargons).where(eq(jargons.id, jargonId)).limit(1).get();
    if (!row) {
      return reply.code(404).send({ detail: "黑话不存在" });
    }
    drizzle.delete(jargons).where(eq(jargons.id, jargonId)).run();
    return { success: true, message: "删除成功", deleted_count: 1 };
  });

  app.post("/api/webui/jargon/batch/delete", async (request, reply) => {
    const parsed = z.object({ ids: z.array(z.number().int()) }).safeParse(request.body ?? {});
    if (!parsed.success || parsed.data.ids.length === 0) {
      return reply.code(400).send({ detail: "ID列表不能为空" });
    }
    const result = drizzle.delete(jargons).where(inArray(jargons.id, parsed.data.ids)).run();
    const deletedCount = Number(result.changes);
    return { success: true, message: `成功删除 ${deletedCount} 条黑话`, deleted_count: deletedCount };
  });

  app.post("/api/webui/jargon/batch/set-jargon", async (request, reply) => {
    const query = request.query as { ids?: string; is_jargon?: string };
    const ids = (query.ids ?? "")
      .split(",")
      .map((value) => Number.parseInt(value, 10))
      .filter((value) => Number.isInteger(value));
    const isJargon = query.is_jargon === "true";
    if (ids.length === 0 || query.is_jargon === undefined) {
      return reply.code(400).send({ detail: "ID列表不能为空" });
    }
    const rows = drizzle.select().from(jargons).where(inArray(jargons.id, ids)).all();
    const now = localDatetimeNow();
    for (const row of rows) {
      drizzle
        .update(jargons)
        .set({ isJargon: isJargon ? 1 : 0, updatedTimestamp: now })
        .where(eq(jargons.id, row.id!))
        .run();
    }
    return { success: true, message: `成功更新 ${rows.length} 条黑话状态` };
  });
}
