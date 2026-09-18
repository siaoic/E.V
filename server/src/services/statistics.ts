/**
 * 统计服务层（对照 src/services/statistics_service.py 迁移）。
 *
 * 聚合口径逐字对齐：
 * - 缓存命中 token = prompt_cache_enabled 时 max(hit, 0)，否则 0；
 * - 缓存未命中 token 按「详细统计相同口径」补全供应商未返回的部分；
 * - 聊天任务 = task_name IN ('replyer','planner')；
 * - 小时/天序列从窗口起点到终点逐桶补零；
 * - 在线时间按区间裁剪合并后拆到小时桶。
 */

import { and, desc, eq, gte, lte, or, sql, type SQL } from "drizzle-orm";

import type { DbHandle } from "../db/client.js";
import { llmUsage, maiMessages, onlineTime } from "../db/schema.js";
import { LocalStore } from "./local-store.js";

export const DASHBOARD_STATISTICS_CACHE_KEY = "webui_dashboard_statistics_cache";
export const DASHBOARD_STATISTICS_CACHE_VERSION = 5;
export const DEFAULT_DASHBOARD_CACHE_MAX_AGE_SECONDS = 20 * 60;

const CACHE_HIT_EXPR = sql`CASE WHEN ${llmUsage.promptCacheEnabled} = 1 THEN max(${llmUsage.promptCacheHitTokens}, 0) ELSE 0 END`;
const CACHE_MISS_EXPR = sql`CASE WHEN ${llmUsage.promptCacheEnabled} = 1 THEN (
  CASE
    WHEN max(${llmUsage.promptCacheMissTokens}, 0) > 0 THEN max(${llmUsage.promptCacheMissTokens}, 0)
    WHEN max(${llmUsage.promptCacheHitTokens}, 0) > 0 THEN max(${llmUsage.promptTokens} - max(${llmUsage.promptCacheHitTokens}, 0), 0)
    WHEN ${llmUsage.promptTokens} > 0 THEN ${llmUsage.promptTokens}
    ELSE 0
  END
) ELSE 0 END`;
const IS_CHAT_TASK = sql`${llmUsage.taskName} IN ('replyer', 'planner')`;

export interface StatisticsSummary {
  total_requests: number;
  total_cost: number;
  total_tokens: number;
  input_tokens: number;
  output_tokens: number;
  cache_hit_tokens: number;
  cache_miss_tokens: number;
  cache_hit_rate: number | null;
  chat_cache_hit_tokens: number;
  chat_cache_miss_tokens: number;
  chat_cache_hit_rate: number | null;
  online_time: number;
  total_messages: number;
  total_replies: number;
  avg_response_time: number;
  cost_per_hour: number;
  tokens_per_hour: number;
}

export interface TimeSeriesItem {
  timestamp: string;
  online_seconds: number;
  requests: number;
  cost: number;
  tokens: number;
  input_tokens: number;
  output_tokens: number;
  cache_hit_tokens: number;
  cache_miss_tokens: number;
}

export interface ModelStatisticsItem {
  model_name: string;
  request_count: number;
  total_cost: number;
  total_tokens: number;
  input_tokens: number;
  output_tokens: number;
  cache_hit_tokens: number;
  cache_miss_tokens: number;
  cache_hit_rate: number | null;
  avg_response_time: number;
}

export interface RecentActivityItem {
  timestamp: string;
  model: string | null;
  request_type: string;
  tokens: number;
  input_tokens: number;
  output_tokens: number;
  cache_hit_tokens: number;
  cache_miss_tokens: number;
  cost: number;
  time_cost: number;
  status: null;
}

function calculateCacheHitRate(hitTokens: number, missTokens: number): number | null {
  const total = hitTokens + missTokens;
  if (total <= 0) {
    return null;
  }
  return hitTokens / total;
}

const pad = (value: number, width = 2) => String(value).padStart(width, "0");

function strftimeLocal(date: Date, withHour: boolean): string {
  return (
    `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}` +
    `T${pad(withHour ? date.getHours() : 0)}:00:00`
  );
}

export function getSummaryStatistics(db: DbHandle, startBound: string, endBound: string): StatisticsSummary {
  const row = db.drizzle
    .select({
      total_requests: sql<number>`count(*)`,
      total_cost: sql<number>`sum(${llmUsage.cost})`,
      input_tokens: sql<number>`sum(${llmUsage.promptTokens})`,
      output_tokens: sql<number>`sum(${llmUsage.completionTokens})`,
      cache_hit_tokens: sql<number>`sum(${CACHE_HIT_EXPR})`,
      cache_miss_tokens: sql<number>`sum(${CACHE_MISS_EXPR})`,
      chat_cache_hit_tokens: sql<number>`sum(CASE WHEN ${IS_CHAT_TASK} THEN ${CACHE_HIT_EXPR} ELSE 0 END)`,
      chat_cache_miss_tokens: sql<number>`sum(CASE WHEN ${IS_CHAT_TASK} THEN ${CACHE_MISS_EXPR} ELSE 0 END)`,
      avg_response_time: sql<number>`avg(${llmUsage.timeCost})`,
    })
    .from(llmUsage)
    .where(and(gte(llmUsage.timestamp, startBound), lte(llmUsage.timestamp, endBound)))
    .get()!;

  const cacheHitTokens = Number(row.cache_hit_tokens ?? 0);
  const cacheMissTokens = Number(row.cache_miss_tokens ?? 0);
  const chatCacheHitTokens = Number(row.chat_cache_hit_tokens ?? 0);
  const chatCacheMissTokens = Number(row.chat_cache_miss_tokens ?? 0);

  const summary: StatisticsSummary = {
    total_requests: Number(row.total_requests ?? 0),
    total_cost: Number(row.total_cost ?? 0),
    input_tokens: Number(row.input_tokens ?? 0),
    output_tokens: Number(row.output_tokens ?? 0),
    total_tokens: Number(row.input_tokens ?? 0) + Number(row.output_tokens ?? 0),
    cache_hit_tokens: cacheHitTokens,
    cache_miss_tokens: cacheMissTokens,
    cache_hit_rate: calculateCacheHitRate(cacheHitTokens, cacheMissTokens),
    chat_cache_hit_tokens: chatCacheHitTokens,
    chat_cache_miss_tokens: chatCacheMissTokens,
    chat_cache_hit_rate: calculateCacheHitRate(chatCacheHitTokens, chatCacheMissTokens),
    online_time: 0,
    total_messages: 0,
    total_replies: 0,
    avg_response_time: Number(row.avg_response_time ?? 0),
    cost_per_hour: 0,
    tokens_per_hour: 0,
  };

  // 在线时间：与窗口相交的区间裁剪后求和
  const onlineRows = db.drizzle
    .select({ start: onlineTime.startTimestamp, end: onlineTime.endTimestamp })
    .from(onlineTime)
    .where(or_(gte(onlineTime.startTimestamp, startBound), gte(onlineTime.endTimestamp, startBound)))
    .all();
  for (const record of onlineRows) {
    const start = Date.parse((record.start ?? "").replace(" ", "T"));
    const end = Date.parse((record.end ?? "").replace(" ", "T"));
    const windowStart = Date.parse(startBound.replace(" ", "T"));
    const windowEnd = Date.parse(endBound.replace(" ", "T"));
    if (Number.isNaN(start) || Number.isNaN(end)) {
      continue;
    }
    const clippedStart = Math.max(start, windowStart);
    const clippedEnd = Math.min(end, windowEnd);
    if (clippedEnd > clippedStart) {
      summary.online_time += (clippedEnd - clippedStart) / 1000;
    }
  }

  // 消息计数（排除 notice；回复 = reply_to 非空）
  const messageWhere = and(
    sql`${maiMessages.messageId} != 'notice'`,
    gte(maiMessages.timestamp, startBound),
    lte(maiMessages.timestamp, endBound),
  );
  summary.total_messages = Number(
    db.drizzle.select({ count: sql<number>`count(*)` }).from(maiMessages).where(messageWhere).get()!.count,
  );
  summary.total_replies = Number(
    db.drizzle
      .select({ count: sql<number>`count(*)` })
      .from(maiMessages)
      .where(and(messageWhere, sql`${maiMessages.replyTo} IS NOT NULL`))
      .get()!.count,
  );

  if (summary.online_time > 0) {
    const onlineHours = summary.online_time / 3600;
    summary.cost_per_hour = summary.total_cost / onlineHours;
    summary.tokens_per_hour = summary.total_tokens / onlineHours;
  }
  return summary;
}

function or_(...conditions: (SQL | undefined)[]): SQL | undefined {
  const valid = conditions.filter((condition): condition is SQL => condition !== undefined);
  return valid.length > 0 ? or(...valid) : undefined;
}

export function getModelStatistics(db: DbHandle, startBound: string, endBound?: string): ModelStatisticsItem[] {
  const modelNameExpr = sql<string>`coalesce(${llmUsage.modelAssignName}, ${llmUsage.modelName}, 'unknown')`;
  const conditions: SQL[] = [gte(llmUsage.timestamp, startBound)];
  if (endBound !== undefined) {
    conditions.push(lte(llmUsage.timestamp, endBound));
  }
  const rows = db.drizzle
    .select({
      model_name: modelNameExpr,
      request_count: sql<number>`count(*)`,
      total_cost: sql<number>`sum(${llmUsage.cost})`,
      input_tokens: sql<number>`sum(${llmUsage.promptTokens})`,
      output_tokens: sql<number>`sum(${llmUsage.completionTokens})`,
      cache_hit_tokens: sql<number>`sum(${CACHE_HIT_EXPR})`,
      cache_miss_tokens: sql<number>`sum(${CACHE_MISS_EXPR})`,
      avg_response_time: sql<number>`avg(${llmUsage.timeCost})`,
    })
    .from(llmUsage)
    .where(and(...conditions))
    .groupBy(modelNameExpr)
    .orderBy(desc(sql`count(*)`))
    .limit(10)
    .all();

  return rows.map((row) => ({
    model_name: row.model_name || "unknown",
    request_count: Number(row.request_count ?? 0),
    total_cost: Number(row.total_cost ?? 0),
    total_tokens: Number(row.input_tokens ?? 0) + Number(row.output_tokens ?? 0),
    input_tokens: Number(row.input_tokens ?? 0),
    output_tokens: Number(row.output_tokens ?? 0),
    cache_hit_tokens: Number(row.cache_hit_tokens ?? 0),
    cache_miss_tokens: Number(row.cache_miss_tokens ?? 0),
    cache_hit_rate: calculateCacheHitRate(Number(row.cache_hit_tokens ?? 0), Number(row.cache_miss_tokens ?? 0)),
    avg_response_time: Number(row.avg_response_time ?? 0),
  }));
}

function getTimeSeries(
  db: DbHandle,
  startBound: string,
  endBound: string,
  bucketExpr: SQL,
  floorTo: "hour" | "day",
): TimeSeriesItem[] {
  const rows = db.drizzle
    .select({
      bucket: bucketExpr,
      requests: sql<number>`count(*)`,
      cost: sql<number>`sum(${llmUsage.cost})`,
      input_tokens: sql<number>`sum(${llmUsage.promptTokens})`,
      output_tokens: sql<number>`sum(${llmUsage.completionTokens})`,
      cache_hit_tokens: sql<number>`sum(${CACHE_HIT_EXPR})`,
      cache_miss_tokens: sql<number>`sum(${CACHE_MISS_EXPR})`,
    })
    .from(llmUsage)
    .where(and(gte(llmUsage.timestamp, startBound), lte(llmUsage.timestamp, endBound)))
    .groupBy(bucketExpr)
    .all();
  const byBucket = new Map(rows.map((row) => [String(row.bucket), row]));

  const stepMs = floorTo === "hour" ? 3600_000 : 24 * 3600_000;
  const result: TimeSeriesItem[] = [];
  let cursor = new Date(startBound.replace(" ", "T"));
  const end = new Date(endBound.replace(" ", "T"));
  if (floorTo === "hour") {
    cursor.setMinutes(0, 0, 0);
  } else {
    cursor.setHours(0, 0, 0, 0);
  }
  while (cursor.getTime() <= end.getTime()) {
    const key = strftimeLocal(cursor, floorTo === "hour");
    const row = byBucket.get(key);
    if (row) {
      const inputTokens = Number(row.input_tokens ?? 0);
      const outputTokens = Number(row.output_tokens ?? 0);
      result.push({
        timestamp: key,
        online_seconds: 0,
        requests: Number(row.requests ?? 0),
        cost: Number(row.cost ?? 0),
        tokens: inputTokens + outputTokens,
        input_tokens: inputTokens,
        output_tokens: outputTokens,
        cache_hit_tokens: Number(row.cache_hit_tokens ?? 0),
        cache_miss_tokens: Number(row.cache_miss_tokens ?? 0),
      });
    } else {
      result.push({
        timestamp: key,
        online_seconds: 0,
        requests: 0,
        cost: 0,
        tokens: 0,
        input_tokens: 0,
        output_tokens: 0,
        cache_hit_tokens: 0,
        cache_miss_tokens: 0,
      });
    }
    cursor = new Date(cursor.getTime() + stepMs);
  }
  return result;
}

export function getHourlyStatistics(db: DbHandle, startBound: string, endBound: string): TimeSeriesItem[] {
  return getTimeSeries(db, startBound, endBound, sql`strftime('%Y-%m-%dT%H:00:00', ${llmUsage.timestamp})`, "hour");
}

export function getDailyStatistics(db: DbHandle, startBound: string, endBound: string): TimeSeriesItem[] {
  return getTimeSeries(db, startBound, endBound, sql`strftime('%Y-%m-%dT00:00:00', ${llmUsage.timestamp})`, "day");
}

/** 在线区间裁剪合并后拆到小时桶（对应 get_hourly_online_seconds）。 */
export function getHourlyOnlineSeconds(db: DbHandle, startBound: string, endBound: string): Map<string, number> {
  const rows = db.drizzle
    .select({ start: onlineTime.startTimestamp, end: onlineTime.endTimestamp })
    .from(onlineTime)
    .all();
  const windowStart = Date.parse(startBound.replace(" ", "T"));
  const windowEnd = Date.parse(endBound.replace(" ", "T"));

  const clipped: Array<[number, number]> = [];
  for (const row of rows) {
    const start = Date.parse((row.start ?? "").replace(" ", "T"));
    const end = Date.parse((row.end ?? "").replace(" ", "T"));
    if (Number.isNaN(start) || Number.isNaN(end)) {
      continue;
    }
    const clippedStart = Math.max(start, windowStart);
    const clippedEnd = Math.min(end, windowEnd);
    if (clippedEnd > clippedStart) {
      clipped.push([clippedStart, clippedEnd]);
    }
  }
  clipped.sort((a, b) => a[0] - b[0]);

  const merged: Array<[number, number]> = [];
  for (const [start, end] of clipped) {
    const previous = merged[merged.length - 1];
    if (previous && start <= previous[1]) {
      previous[1] = Math.max(previous[1], end);
    } else {
      merged.push([start, end]);
    }
  }

  const hourlySeconds = new Map<string, number>();
  for (const [start, end] of merged) {
    let current = start;
    while (current < end) {
      const hourStart = new Date(current);
      hourStart.setMinutes(0, 0, 0);
      const nextHour = hourStart.getTime() + 3600_000;
      const segmentEnd = Math.min(end, nextHour);
      const hourKey = strftimeLocal(hourStart, true);
      hourlySeconds.set(hourKey, (hourlySeconds.get(hourKey) ?? 0) + (segmentEnd - current) / 1000);
      current = segmentEnd;
    }
  }
  return hourlySeconds;
}

function normalizeCacheTokens(
  promptTokens: number,
  cacheEnabled: boolean,
  hitTokens: number,
  missTokens: number,
): [number, number] {
  if (!cacheEnabled) {
    return [0, 0];
  }
  const normalizedHit = Math.max(hitTokens, 0);
  let normalizedMiss = Math.max(missTokens, 0);
  if (normalizedMiss === 0 && normalizedHit > 0) {
    normalizedMiss = Math.max(promptTokens - normalizedHit, 0);
  }
  if (normalizedHit + normalizedMiss === 0 && promptTokens > 0) {
    normalizedMiss = promptTokens;
  }
  return [normalizedHit, normalizedMiss];
}

export function getRecentActivity(db: DbHandle, startBound: string, endBound: string, limit = 10): RecentActivityItem[] {
  const rows = db.drizzle
    .select()
    .from(llmUsage)
    .where(and(gte(llmUsage.timestamp, startBound), lte(llmUsage.timestamp, endBound)))
    .orderBy(desc(llmUsage.timestamp))
    .limit(limit)
    .all();
  return rows.map((record) => {
    const inputTokens = record.promptTokens;
    const outputTokens = record.completionTokens;
    const [cacheHitTokens, cacheMissTokens] = normalizeCacheTokens(
      inputTokens,
      record.promptCacheEnabled,
      record.promptCacheHitTokens,
      record.promptCacheMissTokens,
    );
    return {
      timestamp: (record.timestamp ?? "").replace(" ", "T"),
      model: record.modelAssignName || record.modelName,
      request_type: record.requestType,
      tokens: inputTokens + outputTokens,
      input_tokens: inputTokens,
      output_tokens: outputTokens,
      cache_hit_tokens: cacheHitTokens,
      cache_miss_tokens: cacheMissTokens,
      cost: record.cost ?? 0,
      time_cost: record.timeCost ?? 0,
      status: null,
    };
  });
}

export interface DashboardData {
  summary: StatisticsSummary;
  model_stats: ModelStatisticsItem[];
  hourly_data: TimeSeriesItem[];
  daily_data: TimeSeriesItem[];
  recent_activity: RecentActivityItem[];
}

export function computeDashboardStatistics(db: DbHandle, hours: number): DashboardData {
  const end = new Date();
  const start = new Date(end.getTime() - hours * 3600_000);
  const startBound = toBound(start);
  const endBound = toBound(end);

  const summary = getSummaryStatistics(db, startBound, endBound);
  const model_stats = getModelStatistics(db, startBound, endBound);
  const hourly_data = getHourlyStatistics(db, startBound, endBound);
  const hourlyOnline = getHourlyOnlineSeconds(db, startBound, endBound);
  for (const item of hourly_data) {
    item.online_seconds = hourlyOnline.get(item.timestamp) ?? 0;
  }
  const daily_data = getDailyStatistics(db, startBound, endBound);
  const recent_activity = getRecentActivity(db, startBound, endBound, 10);
  return { summary, model_stats, hourly_data, daily_data, recent_activity };
}

function toBound(date: Date): string {
  const pad = (value: number, width = 2) => String(value).padStart(width, "0");
  return (
    `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}` +
    ` ${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}.000000`
  );
}

// ==================== 仪表盘缓存（data/local_store.json） ====================

function isEmptyTimeSeriesItem(item: TimeSeriesItem): boolean {
  return (
    item.requests === 0 && item.cost === 0 && item.tokens === 0 && item.online_seconds === 0
  );
}

function compactEntry(data: DashboardData): Record<string, unknown> {
  const entry: Record<string, unknown> = JSON.parse(JSON.stringify(data));
  entry.hourly_data = (entry.hourly_data as TimeSeriesItem[]).filter((item) => !isEmptyTimeSeriesItem(item));
  entry.daily_data = (entry.daily_data as TimeSeriesItem[]).filter((item) => !isEmptyTimeSeriesItem(item));
  entry.sparse = true;
  return entry;
}

function expandTimeSeries(
  sparseSeries: unknown,
  startTime: Date,
  endTime: Date,
  stepMs: number,
  withHour: boolean,
): TimeSeriesItem[] {
  const sparseItems = Array.isArray(sparseSeries) ? sparseSeries : [];
  const byTimestamp = new Map<string, TimeSeriesItem>();
  for (const item of sparseItems) {
    if (item && typeof item === "object" && typeof (item as TimeSeriesItem).timestamp === "string") {
      byTimestamp.set((item as TimeSeriesItem).timestamp, item as TimeSeriesItem);
    }
  }
  const result: TimeSeriesItem[] = [];
  const cursor = new Date(startTime.getTime());
  if (withHour) {
    cursor.setMinutes(0, 0, 0);
  } else {
    cursor.setHours(0, 0, 0, 0);
  }
  while (cursor.getTime() <= endTime.getTime()) {
    const key = strftimeLocal(cursor, withHour);
    result.push(
      byTimestamp.get(key) ?? {
        timestamp: key,
        online_seconds: 0,
        requests: 0,
        cost: 0,
        tokens: 0,
        input_tokens: 0,
        output_tokens: 0,
        cache_hit_tokens: 0,
        cache_miss_tokens: 0,
      },
    );
    cursor.setTime(cursor.getTime() + stepMs);
  }
  return result;
}

export function getCachedDashboardStatistics(
  store: LocalStore,
  hours: number,
  maxAgeSeconds = DEFAULT_DASHBOARD_CACHE_MAX_AGE_SECONDS,
): DashboardData | null {
  const rawCache = store.get<Record<string, unknown>>(DASHBOARD_STATISTICS_CACHE_KEY);
  if (!rawCache || typeof rawCache !== "object") {
    return null;
  }
  if (rawCache.version !== DASHBOARD_STATISTICS_CACHE_VERSION) {
    return null;
  }
  const generatedAt = rawCache.generated_at;
  if (typeof generatedAt !== "number") {
    return null;
  }
  if (Date.now() / 1000 - generatedAt > maxAgeSeconds) {
    return null;
  }
  const entries = rawCache.entries;
  if (!entries || typeof entries !== "object") {
    return null;
  }
  const entry = (entries as Record<string, unknown>)[String(hours)];
  if (!entry || typeof entry !== "object") {
    return null;
  }

  try {
    const record = entry as Record<string, unknown>;
    if (record.sparse !== true) {
      return record as unknown as DashboardData;
    }
    const generatedDate = new Date(generatedAt * 1000);
    const expanded: Record<string, unknown> = { ...record };
    expanded.hourly_data = expandTimeSeries(
      record.hourly_data,
      new Date(generatedDate.getTime() - hours * 3600_000),
      generatedDate,
      3600_000,
      true,
    );
    expanded.daily_data = expandTimeSeries(
      record.daily_data,
      new Date(generatedDate.getTime() - hours * 3600_000),
      generatedDate,
      24 * 3600_000,
      false,
    );
    delete expanded.sparse;
    return expanded as unknown as DashboardData;
  } catch {
    return null;
  }
}

export function updateDashboardStatisticsCacheEntry(
  store: LocalStore,
  hours: number,
  data: DashboardData,
): void {
  const rawCache = store.get<Record<string, unknown>>(DASHBOARD_STATISTICS_CACHE_KEY);
  const entries: Record<string, unknown> =
    rawCache && typeof rawCache === "object" && rawCache.entries && typeof rawCache.entries === "object"
      ? { ...(rawCache.entries as Record<string, unknown>) }
      : {};
  entries[String(hours)] = compactEntry(data);
  store.set(DASHBOARD_STATISTICS_CACHE_KEY, {
    version: DASHBOARD_STATISTICS_CACHE_VERSION,
    generated_at: Date.now() / 1000,
    entries,
  });
}

export function getDashboardStatistics(db: DbHandle, store: LocalStore, hours: number): DashboardData {
  const cached = getCachedDashboardStatistics(store, hours);
  if (cached) {
    return cached;
  }
  const data = computeDashboardStatistics(db, hours);
  updateDashboardStatisticsCacheEntry(store, hours, data);
  return data;
}
