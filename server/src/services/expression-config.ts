/**
 * 表达方式配置工具（对照 src/common/utils/utils_config.py 的
 * ExpressionConfigUtils / ChatConfigUtils 目标解析部分）。
 *
 * 目标匹配优先级（_find_expression_config_item）：
 * 通配(*) > 精确(platform+item_id+type) > 平台兜底(platform, 无 item_id) > 全局默认(全空)。
 * 通配/精确匹配需要聊天流的 group_id/user_id 归属（经 chat_sessions 表）。
 */

import { existsSync, readFileSync } from "node:fs";
import path from "node:path";

import { and, eq } from "drizzle-orm";
import { parse as parseToml } from "smol-toml";

import type { DbHandle } from "../db/client.js";
import { chatSessions } from "../db/schema.js";

export interface TargetItem {
  platform?: string;
  item_id?: string;
  type?: string;
  rule_type?: string;
  use?: boolean;
  learn?: boolean;
}

export function targetValues(item: TargetItem): { platform: string; itemId: string; ruleType: string } {
  if (typeof (item as Record<string, unknown>).get === "function") {
    // 防御：TOML 解析结果是普通对象，不会走到这里
  }
  return {
    platform: String(item.platform ?? "").trim(),
    itemId: String(item.item_id ?? "").trim(),
    ruleType: String(item.type ?? item.rule_type ?? "").trim(),
  };
}

export function isDefaultTarget(item: TargetItem): boolean {
  const { platform, itemId } = targetValues(item);
  return platform === "" && itemId === "";
}

export function isPlatformDefaultTarget(item: TargetItem): boolean {
  const { platform, itemId } = targetValues(item);
  return platform !== "" && platform !== "*" && itemId === "";
}

export function isWildcardTarget(item: TargetItem): boolean {
  const { platform, itemId } = targetValues(item);
  return platform === "*" || itemId === "*";
}

/** 解析表达式向量索引路径（对应 resolve_project_path）。 */
export function resolveExpressionVectorIndexPath(rootDir: string, rawPath: string): string {
  const normalized = String(rawPath ?? "")
    .split(/\s+/)
    .join(" ")
    .trim();
  return path.isAbsolute(normalized) ? path.resolve(normalized) : path.resolve(rootDir, normalized);
}

function readExpressionSection(rootDir: string): Record<string, unknown> {
  const filePath = path.join(rootDir, "config", "bot_config.toml");
  if (!existsSync(filePath)) {
    return {};
  }
  try {
    const parsed = parseToml(readFileSync(filePath, "utf8")) as { expression?: Record<string, unknown> };
    return parsed.expression ?? {};
  } catch {
    return {};
  }
}

export function readExpressionVectorIndexPath(rootDir: string): string {
  const section = readExpressionSection(rootDir);
  const raw = section.expression_vector_index_path;
  return resolveExpressionVectorIndexPath(
    rootDir,
    typeof raw === "string" && raw.trim() ? raw : "data/expression_selection/expression_vector_index.json",
  );
}

export function readExpressionGroups(rootDir: string): Array<{ targets: TargetItem[] }> {
  const section = readExpressionSection(rootDir);
  const groups = section.expression_groups;
  if (!Array.isArray(groups)) {
    return [];
  }
  return groups.map((group) => ({
    targets: Array.isArray((group as { targets?: TargetItem[] }).targets)
      ? ((group as { targets: TargetItem[] }).targets ?? [])
      : [],
  }));
}

export function readExpressionLearningList(rootDir: string): TargetItem[] {
  const section = readExpressionSection(rootDir);
  const list = section.learning_list;
  return Array.isArray(list) ? (list as TargetItem[]) : [];
}

// ==================== 聊天流解析 ====================

interface ChatStreamSnapshot {
  platform: string;
  groupId: string | null;
  userId: string | null;
  isGroupSession: boolean;
}

function chatStreamSnapshot(db: DbHandle, sessionId: string): ChatStreamSnapshot | null {
  const row = db.drizzle.select().from(chatSessions).where(eq(chatSessions.sessionId, sessionId)).limit(1).get();
  if (!row) {
    return null;
  }
  return {
    platform: String(row.platform ?? "").trim(),
    groupId: row.groupId,
    userId: row.userId,
    isGroupSession: Boolean(row.groupId),
  };
}

/** 对应 chat_manager.resolve_session_ids_by_target 的 DB 路径。 */
export function resolveSessionIdsByTarget(
  db: DbHandle,
  platform: string,
  targetId: string,
  chatType: string,
): Set<string> {
  const normalizedPlatform = platform.trim();
  const normalizedTargetId = targetId.trim();
  if (chatType !== "group" && chatType !== "private") {
    return new Set();
  }
  const targetAttr = chatType === "group" ? chatSessions.groupId : chatSessions.userId;
  const rows = db.drizzle
    .select({ sessionId: chatSessions.sessionId })
    .from(chatSessions)
    .where(and(eq(chatSessions.platform, normalizedPlatform), eq(targetAttr, normalizedTargetId)))
    .all();
  return new Set(rows.map((row) => row.sessionId));
}

/** 非通配精确目标 → 已知聊天流（get_target_session_ids）。 */
export function getTargetSessionIds(db: DbHandle, item: TargetItem): Set<string> {
  const { platform, itemId, ruleType } = targetValues(item);
  if (!platform || !itemId) {
    return new Set();
  }
  return resolveSessionIdsByTarget(db, platform, itemId, ruleType);
}

/** 通配目标 → 已知聊天流（get_target_session_ids_with_wildcards）。 */
export function getTargetSessionIdsWithWildcards(db: DbHandle, item: TargetItem): Set<string> {
  const { platform, itemId, ruleType } = targetValues(item);
  if (!platform || !itemId) {
    return new Set();
  }
  if (!isWildcardTarget(item)) {
    return getTargetSessionIds(db, item);
  }
  if (ruleType !== "group" && ruleType !== "private") {
    return new Set();
  }
  const targetAttr = ruleType === "group" ? chatSessions.groupId : chatSessions.userId;
  const conditions = [];
  if (platform !== "*") {
    conditions.push(eq(chatSessions.platform, platform));
  }
  if (itemId !== "*") {
    conditions.push(eq(targetAttr, itemId));
  }
  const matched = new Set<string>();
  for (const row of db.drizzle
    .select({ sessionId: chatSessions.sessionId })
    .from(chatSessions)
    .where(conditions.length > 0 ? and(...conditions) : undefined)
    .all()) {
    matched.add(row.sessionId);
  }
  return matched;
}

function targetMatchesSessionWithWildcards(db: DbHandle, item: TargetItem, sessionId: string): boolean {
  const { platform, itemId, ruleType } = targetValues(item);
  if (!sessionId || !platform || !itemId) {
    return false;
  }
  if (ruleType !== "group" && ruleType !== "private") {
    return false;
  }
  const stream = chatStreamSnapshot(db, sessionId);
  if (!stream) {
    return false; // 通配目标在内存/库里都找不到聊天流时不命中（原实现同）
  }
  const streamTargetId = String((ruleType === "group" ? stream.groupId : stream.userId) ?? "").trim();
  if (!streamTargetId) {
    return false;
  }
  const platformMatches = platform === "*" || stream.platform === platform;
  const itemMatches = itemId === "*" || streamTargetId === itemId;
  return platformMatches && itemMatches;
}

function targetMatchesSession(db: DbHandle, item: TargetItem, sessionId: string): boolean {
  const { platform, itemId, ruleType } = targetValues(item);
  if (!sessionId || !platform || !itemId) {
    return false;
  }
  if (ruleType !== "group" && ruleType !== "private") {
    return false;
  }
  const stream = chatStreamSnapshot(db, sessionId);
  if (!stream) {
    return false;
  }
  const streamTargetId = String((ruleType === "group" ? stream.groupId : stream.userId) ?? "").trim();
  return stream.platform === platform && streamTargetId === itemId;
}

function platformDefaultMatchesSession(db: DbHandle, item: TargetItem, sessionId: string): boolean {
  const { platform, itemId, ruleType } = targetValues(item);
  if (!sessionId || !platform || platform === "*" || itemId) {
    return false;
  }
  if (ruleType !== "group" && ruleType !== "private") {
    return false;
  }
  const configIsGroup = ruleType === "group";
  const stream = chatStreamSnapshot(db, sessionId);
  if (!stream) {
    return false;
  }
  return stream.platform === platform && stream.isGroupSession === configIsGroup;
}

/** 表达配置按会话匹配（_find_expression_config_item 的优先级级联）。 */
export function getExpressionConfigForChat(
  db: DbHandle,
  rootDir: string,
  sessionId: string | null,
): { useExpression: boolean; enableLearning: boolean } {
  const learningList = readExpressionLearningList(rootDir);
  if (learningList.length === 0) {
    return { useExpression: true, enableLearning: true };
  }
  let matched: TargetItem | undefined;
  if (sessionId) {
    matched = learningList.find((item) => isWildcardTarget(item) && targetMatchesSessionWithWildcards(db, item, sessionId));
    if (!matched) {
      matched = learningList.find(
        (item) =>
          !isDefaultTarget(item) && !isWildcardTarget(item) && !isPlatformDefaultTarget(item) && targetMatchesSession(db, item, sessionId),
      );
    }
    if (!matched) {
      matched = learningList.find((item) => isPlatformDefaultTarget(item) && platformDefaultMatchesSession(db, item, sessionId));
    }
  }
  if (!matched) {
    matched = learningList.find((item) => isDefaultTarget(item));
  }
  if (!matched) {
    return { useExpression: true, enableLearning: true };
  }
  return { useExpression: matched.use !== false, enableLearning: matched.learn !== false };
}
