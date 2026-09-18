/**
 * 统计路由（对照 src/webui/routers/statistics.py）：
 * - GET /statistics/detailed  → 503（详细统计快照由 HTML 报告生成写入，TS 侧暂无生成器，如实 503）
 * - GET /statistics/dashboard → DashboardData（带 local_store 20 分钟缓存与稀疏展开）
 * - GET /statistics/summary   → StatisticsSummary
 * - GET /statistics/models    → ModelStatistics[]
 */

import { z } from "zod";
import type { FastifyInstance } from "fastify";

import type { DbHandle } from "../../db/client.js";
import { LocalStore } from "../../services/local-store.js";
import {
  getDashboardStatistics,
  getModelStatistics,
  getSummaryStatistics,
} from "../../services/statistics.js";

export interface StatisticsRouteDeps {
  db: DbHandle;
  localStore: LocalStore;
}

const hoursSchema = z.coerce.number().int().min(1).default(24);

export function registerStatisticsRoutes(app: FastifyInstance, deps: StatisticsRouteDeps): void {
  app.get("/api/webui/statistics/detailed", async (_request, reply) => {
    return reply.code(503).send({ detail: "详细统计正在生成，请稍后重试" });
  });

  app.get("/api/webui/statistics/dashboard", async (request, reply) => {
    const parsed = hoursSchema.safeParse((request.query as { hours?: string }).hours ?? undefined);
    if (!parsed.success) {
      return reply.code(422).send({ detail: "hours 必须是正整数" });
    }
    return getDashboardStatistics(deps.db, deps.localStore, parsed.data);
  });

  app.get("/api/webui/statistics/summary", async (request, reply) => {
    const parsed = hoursSchema.safeParse((request.query as { hours?: string }).hours ?? undefined);
    if (!parsed.success) {
      return reply.code(422).send({ detail: "hours 必须是正整数" });
    }
    const end = new Date();
    const start = new Date(end.getTime() - parsed.data * 3600_000);
    return getSummaryStatistics(deps.db, toBound(start), toBound(end));
  });

  app.get("/api/webui/statistics/models", async (request, reply) => {
    const parsed = hoursSchema.safeParse((request.query as { hours?: string }).hours ?? undefined);
    if (!parsed.success) {
      return reply.code(422).send({ detail: "hours 必须是正整数" });
    }
    const end = new Date();
    const start = new Date(end.getTime() - parsed.data * 3600_000);
    return getModelStatistics(deps.db, toBound(start), toBound(end));
  });
}

function toBound(date: Date): string {
  const pad = (value: number, width = 2) => String(value).padStart(width, "0");
  return (
    `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}` +
    ` ${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}.000000`
  );
}
