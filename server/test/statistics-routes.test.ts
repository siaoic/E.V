// statistics 对拍：摘要聚合 / 缓存 token 归一化 / 在线时间裁剪 / 序列补零 / dashboard 缓存

import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import path from "node:path";

import { makeFixtureRepo, makeTestApp } from "./helpers.js";
import { llmUsage, maiMessages, onlineTime } from "../src/db/schema.js";
import { sqliteDatetime } from "../src/db/datetime.js";

describe("statistics 路由", () => {
  const repo = makeFixtureRepo();
  const { app, tokenManager, db } = makeTestApp({ repo });
  const token = tokenManager.getToken();
  const auth = { cookies: { maibot_session: token } };

  const HOUR = 3600_000;
  const now = Date.now();
  const at = (hoursAgo: number) => new Date(now - hoursAgo * HOUR);

  beforeAll(() => {
    // LLM 用量：
    // - 回复任务（replyer）带 prompt cache：hit 300 / miss 0 / prompt 500 → miss 归一化 = 200
    // - 非聊天任务（summary）缓存未启用 → 缓存贡献 0
    db.drizzle
      .insert(llmUsage)
      .values([
        {
          modelName: "gpt-x",
          modelAssignName: "主力模型",
          modelApiProviderName: "openai",
          sessionId: "qq:group:111",
          taskName: "replyer",
          requestType: "reply",
          timeCost: 2,
          timestamp: sqliteDatetime(at(2)),
          promptTokens: 500,
          completionTokens: 200,
          totalTokens: 700,
          promptCacheEnabled: true,
          promptCacheHitTokens: 300,
          promptCacheMissTokens: 0,
          cost: 0.5,
        },
        {
          modelName: "gemini-y",
          modelAssignName: null,
          modelApiProviderName: "google",
          sessionId: "",
          taskName: "summary",
          requestType: "memory",
          timeCost: 1,
          timestamp: sqliteDatetime(at(1)),
          promptTokens: 100,
          completionTokens: 50,
          totalTokens: 150,
          promptCacheEnabled: false,
          promptCacheHitTokens: 0,
          promptCacheMissTokens: 0,
          cost: 0.1,
        },
        {
          // 窗口外（30 小时前）：不应计入任何默认统计
          modelName: "old",
          modelAssignName: null,
          modelApiProviderName: "p",
          sessionId: "",
          taskName: "replyer",
          requestType: "reply",
          timeCost: 9,
          timestamp: sqliteDatetime(at(30)),
          promptTokens: 1000,
          completionTokens: 1000,
          totalTokens: 2000,
          promptCacheEnabled: false,
          promptCacheHitTokens: 0,
          promptCacheMissTokens: 0,
          cost: 9,
        },
      ])
      .run();

    // 在线时间：[25.5h 前, 24.5h 前]（窗口外半段被裁剪）+ [2h 前, 1h 前]
    db.drizzle
      .insert(onlineTime)
      .values([
        {
          timestamp: sqliteDatetime(at(25.5)),
          durationMinutes: 60,
          startTimestamp: sqliteDatetime(at(25.5)),
          endTimestamp: sqliteDatetime(at(24.5)),
        },
        {
          timestamp: sqliteDatetime(at(2)),
          durationMinutes: 60,
          startTimestamp: sqliteDatetime(at(2)),
          endTimestamp: sqliteDatetime(at(1)),
        },
      ])
      .run();

    // 消息：3 条普通 + 1 条回复 + 1 条 notice（不计入）+ 1 条窗口外
    const messageBase = {
      platform: "qq",
      userNickname: "u",
      isMentioned: false,
      isAt: false,
      isEmoji: false,
      isPicture: false,
      isCommand: false,
      isNotify: false,
      rawContent: Buffer.from("x"),
    };
    db.drizzle
      .insert(maiMessages)
      .values([
        { ...messageBase, messageId: "m1", userId: "1", timestamp: sqliteDatetime(at(3)), sessionId: "s1" },
        { ...messageBase, messageId: "m2", userId: "1", timestamp: sqliteDatetime(at(2.5)), sessionId: "s1" },
        {
          ...messageBase,
          messageId: "m3",
          userId: "1",
          timestamp: sqliteDatetime(at(1.5)),
          sessionId: "s1",
          replyTo: "m1",
        },
        {
          ...messageBase,
          messageId: "notice",
          userId: "1",
          timestamp: sqliteDatetime(at(1.2)),
          sessionId: "s1",
        },
        {
          ...messageBase,
          messageId: "m4",
          userId: "1",
          timestamp: sqliteDatetime(at(40)),
          sessionId: "s1",
        },
      ])
      .run();
  });

  afterAll(() => {
    db.close();
  });

  it("summary：聚合、缓存归一化与聊天任务切分", async () => {
    const res = await app.inject({ method: "GET", url: "/api/webui/statistics/summary?hours=24", ...auth });
    expect(res.statusCode).toBe(200);
    const body = res.json() as Record<string, unknown>;
    expect(body.total_requests).toBe(2);
    expect(body.total_cost).toBeCloseTo(0.6);
    expect(body.input_tokens).toBe(600);
    expect(body.output_tokens).toBe(250);
    expect(body.total_tokens).toBe(850);
    // 非聊天任务不贡献缓存
    expect(body.cache_hit_tokens).toBe(300);
    // miss=0 且 hit>0 → 按 prompt-hit 补全 = 200
    expect(body.cache_miss_tokens).toBe(200);
    expect(body.cache_hit_rate).toBeCloseTo(300 / 500);
    // 聊天任务只有第一条
    expect(body.chat_cache_hit_tokens).toBe(300);
    expect(body.chat_cache_miss_tokens).toBe(200);
    expect(body.avg_response_time).toBeCloseTo(1.5);
    // 消息计数：排除 notice 与窗口外
    expect(body.total_messages).toBe(3);
    expect(body.total_replies).toBe(1);
    // 在线时间：第一条 [25.5h, 24.5h] 与窗口 [now-24h, now] 无交集被裁掉；第二条整 1 小时
    expect(body.online_time).toBeCloseTo(3600);
    expect(body.cost_per_hour).toBeGreaterThan(0);
  });

  it("models：coalesce 命名、分组计数与倒序", async () => {
    const res = await app.inject({ method: "GET", url: "/api/webui/statistics/models?hours=24", ...auth });
    const body = res.json() as Array<{ model_name: string; request_count: number; total_tokens: number }>;
    expect(res.statusCode).toBe(200);
    expect(body).toHaveLength(2);
    expect(body[0]!.model_name).toBe("主力模型");
    expect(body[0]!.request_count).toBe(1);
    expect(body[1]!.model_name).toBe("gemini-y"); // assign 为 null → 落到 model_name
    expect(body[1]!.total_tokens).toBe(150);
  });

  it("dashboard：时间序列补零 + 在线秒数合并 + recent_activity", async () => {
    const res = await app.inject({ method: "GET", url: "/api/webui/statistics/dashboard?hours=24", ...auth });
    expect(res.statusCode).toBe(200);
    const body = res.json() as {
      summary: Record<string, unknown>;
      hourly_data: Array<{ timestamp: string; requests: number; online_seconds: number }>;
      daily_data: Array<{ timestamp: string; requests: number }>;
      recent_activity: Array<Record<string, unknown>>;
    };

    // 24 小时窗口 → 25 个小时桶（含两端），其中 2 个有请求
    expect(body.hourly_data).toHaveLength(25);
    const nonEmpty = body.hourly_data.filter((item) => item.requests > 0);
    expect(nonEmpty).toHaveLength(2);
    // 在线区间 [2h, 1h] 跨小时桶：两桶之和应为整段 3600 秒（单桶可能被边界切开）
    const onlineSum = body.hourly_data.reduce((sum, item) => sum + item.online_seconds, 0);
    expect(onlineSum).toBeCloseTo(3600);
    // 序列键格式与 Python strftime 一致
    expect(body.hourly_data[0]!.timestamp).toMatch(/^\d{4}-\d{2}-\d{2}T\d{2}:00:00$/);
    // daily：今天一条（两条记录同一天）
    expect(body.daily_data.length).toBeGreaterThanOrEqual(1);
    const todayTotal = body.daily_data.reduce((sum, item) => sum + item.requests, 0);
    expect(todayTotal).toBe(2);
    // recent_activity：2 条、模型回落与缓存归一化
    expect(body.recent_activity).toHaveLength(2);
    expect(body.recent_activity[0]!.model).toBe("gemini-y");
    expect(body.recent_activity[1]!.model).toBe("主力模型");
    expect(body.recent_activity[1]!.cache_miss_tokens).toBe(200);
    expect(body.recent_activity[1]!.status).toBeNull();
  });

  it("dashboard 缓存：local_store 写入 version=5 与稀疏条目，二次读取走缓存", async () => {
    const storePath = path.join(repo.rootDir, "data", "local_store.json");
    const raw = JSON.parse(readFileSync(storePath, "utf8")) as {
      webui_dashboard_statistics_cache: { version: number; entries: Record<string, { sparse?: boolean }> };
    };
    const cache = raw.webui_dashboard_statistics_cache;
    expect(cache.version).toBe(5);
    expect(cache.entries["24"]).toBeDefined();
    // 全零时间桶被压缩掉
    expect((cache.entries["24"]!.hourly_data as unknown[]).length).toBeLessThan(25);

    // 再请求一次：缓存命中路径也应返回完整 25 桶（稀疏展开）
    const second = await app.inject({ method: "GET", url: "/api/webui/statistics/dashboard?hours=24", ...auth });
    expect((second.json() as { hourly_data: unknown[] }).hourly_data).toHaveLength(25);
  });

  it("detailed：未生成快照时 503（与 Python 进程内快照语义一致）", async () => {
    const res = await app.inject({ method: "GET", url: "/api/webui/statistics/detailed", ...auth });
    expect(res.statusCode).toBe(503);
    expect(res.json()).toEqual({ detail: "详细统计正在生成，请稍后重试" });
  });
});
