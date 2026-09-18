/**
 * 表达方式 AI 审核日志存储（对照 src/learners/expression_review_store.py）。
 *
 * - 日志文件：logs/expression_review/review_logs.json（JSON 数组，整文件重写）
 * - 事件类型：ai_review / manual_rescue（救回通过 review_log_id 关联）
 * - 审核状态（checked/rejected 快照）存于 local_storage 键
 *   `expression_review:{id}`，本模块同样提供读写。
 */

import { randomUUID } from "node:crypto";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";

import type { FastifyBaseLogger } from "fastify";

import type { LocalStore } from "./local-store.js";

const AI_REVIEW_EVENT = "ai_review";
const MANUAL_RESCUE_EVENT = "manual_rescue";

export type ReviewLogEntry = Record<string, unknown>;

export class ExpressionReviewStore {
  constructor(
    readonly logPath: string,
    private readonly localStore: LocalStore,
    private readonly logger: FastifyBaseLogger,
  ) {}

  static open(rootDir: string, localStore: LocalStore, logger: FastifyBaseLogger): ExpressionReviewStore {
    return new ExpressionReviewStore(
      path.join(rootDir, "logs", "expression_review", "review_logs.json"),
      localStore,
      logger,
    );
  }

  private readEntries(): ReviewLogEntry[] {
    if (!existsSync(this.logPath)) {
      return [];
    }
    try {
      const parsed = JSON.parse(readFileSync(this.logPath, "utf8")) as unknown;
      if (!Array.isArray(parsed)) {
        this.logger.warn("表达方式审核日志格式异常，应为 JSON 数组");
        return [];
      }
      return parsed.filter((entry): entry is ReviewLogEntry => entry !== null && typeof entry === "object");
    } catch (error) {
      this.logger.warn({ error }, "表达方式审核日志 JSON 解析失败，已忽略当前文件");
      return [];
    }
  }

  private appendEntry(entry: ReviewLogEntry): void {
    const entries = this.readEntries();
    entries.push(entry);
    mkdirSync(path.dirname(this.logPath), { recursive: true });
    writeFileSync(this.logPath, `${JSON.stringify(entries, null, 2)}\n`, "utf8");
  }

  private nowIso(): string {
    const offsetMinutes = -new Date().getTimezoneOffset();
    const sign = offsetMinutes >= 0 ? "+" : "-";
    const pad = (value: number, width = 2) => String(value).padStart(width, "0");
    const d = new Date();
    return (
      `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}` +
      `T${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}` +
      `${sign}${pad(Math.abs(offsetMinutes) / 60 | 0)}:${pad(Math.abs(offsetMinutes) % 60)}`
    );
  }

  appendAiReviewLog(input: {
    session_id: string;
    situation: string;
    style: string;
    passed: boolean;
    reason: string;
    source: string;
    expression_id?: number | null;
    error?: string | null;
  }): ReviewLogEntry {
    const entry: ReviewLogEntry = {
      id: randomUUID().replaceAll("-", ""),
      event: AI_REVIEW_EVENT,
      created_at: this.nowIso(),
      expression_id: input.expression_id ?? null,
      session_id: String(input.session_id ?? "").trim(),
      passed: Boolean(input.passed),
      reason: String(input.reason ?? "").trim(),
      situation: String(input.situation ?? "").trim(),
      style: String(input.style ?? "").trim(),
      source: input.source,
    };
    if (input.error) {
      entry.error = String(input.error);
    }
    this.appendEntry(entry);
    return entry;
  }

  appendManualRescueLog(reviewLogId: string, expressionId: number): ReviewLogEntry {
    const entry: ReviewLogEntry = {
      id: randomUUID().replaceAll("-", ""),
      event: MANUAL_RESCUE_EVENT,
      created_at: this.nowIso(),
      review_log_id: String(reviewLogId),
      expression_id: expressionId,
    };
    this.appendEntry(entry);
    return entry;
  }

  private parseCreatedAt(value: unknown): number {
    if (typeof value !== "string" || value === "") {
      return 0;
    }
    const parsed = Date.parse(value);
    return Number.isNaN(parsed) ? 0 : parsed;
  }

  private rescueByReviewId(entries: ReviewLogEntry[]): Map<string, ReviewLogEntry> {
    const rescues = entries
      .filter(
        (entry) =>
          entry.event === MANUAL_RESCUE_EVENT && String(entry.review_log_id ?? "").trim() !== "",
      )
      .sort((a, b) => this.parseCreatedAt(b.created_at) - this.parseCreatedAt(a.created_at));
    const map = new Map<string, ReviewLogEntry>();
    for (const entry of rescues) {
      const reviewLogId = String(entry.review_log_id ?? "").trim();
      if (!map.has(reviewLogId)) {
        map.set(reviewLogId, entry);
      }
    }
    return map;
  }

  private withRescueState(entry: ReviewLogEntry, rescueEntry?: ReviewLogEntry): ReviewLogEntry {
    return {
      ...entry,
      rescued: rescueEntry !== undefined,
      rescued_expression_id: rescueEntry ? (rescueEntry.expression_id ?? null) : null,
      rescued_at: rescueEntry ? (rescueEntry.created_at ?? null) : null,
    };
  }

  getRecentAiReviewLogs(options: {
    limit?: number;
    passed?: boolean | null;
    sessionId?: string | null;
  }): ReviewLogEntry[] {
    const { limit = 50, passed = null, sessionId = null } = options;
    const entries = this.readEntries();
    const rescues = this.rescueByReviewId(entries);
    const normalizedSessionId = String(sessionId ?? "").trim();

    let reviewEntries = entries.filter(
      (entry) => (entry.event ?? AI_REVIEW_EVENT) === AI_REVIEW_EVENT && String(entry.id ?? "").trim() !== "",
    );
    if (passed !== null) {
      reviewEntries = reviewEntries.filter((entry) => Boolean(entry.passed) === passed);
    }
    if (normalizedSessionId) {
      reviewEntries = reviewEntries.filter(
        (entry) => String(entry.session_id ?? "").trim() === normalizedSessionId,
      );
    }
    reviewEntries.sort((a, b) => this.parseCreatedAt(b.created_at) - this.parseCreatedAt(a.created_at));
    return reviewEntries
      .slice(0, Math.max(1, limit))
      .map((entry) => this.withRescueState(entry, rescues.get(String(entry.id))));
  }

  getAiReviewLog(reviewLogId: string): ReviewLogEntry | null {
    const normalizedId = String(reviewLogId ?? "").trim();
    if (!normalizedId) {
      return null;
    }
    const entries = this.readEntries();
    const rescues = this.rescueByReviewId(entries);
    for (const entry of entries) {
      if ((entry.event ?? AI_REVIEW_EVENT) !== AI_REVIEW_EVENT) {
        continue;
      }
      if (String(entry.id ?? "").trim() === normalizedId) {
        return this.withRescueState(entry, rescues.get(normalizedId));
      }
    }
    return null;
  }

  // ------------------------------------------------------------------ 审核状态（local_storage）

  getReviewState(expressionId: number | null): { checked: boolean; rejected: boolean; modified_by: string | null } {
    if (expressionId === null) {
      return { checked: false, rejected: false, modified_by: null };
    }
    const value = this.localStore.get<Record<string, unknown>>(`expression_review:${expressionId}`);
    if (value && typeof value === "object") {
      return {
        checked: Boolean(value.checked),
        rejected: Boolean(value.rejected),
        modified_by: (value.modified_by as string | undefined) ?? null,
      };
    }
    return { checked: false, rejected: false, modified_by: null };
  }

  setReviewState(expressionId: number | null, checked: boolean, rejected: boolean, modifiedBy: string | null): void {
    if (expressionId === null) {
      return;
    }
    this.localStore.set(`expression_review:${expressionId}`, {
      checked,
      rejected,
      modified_by: modifiedBy,
    });
  }
}
