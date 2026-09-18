// jargon 路由对拍：列表筛选/聊天名解析/统计/创建去重与 AI 替换/导出导入/批量操作

import { afterAll, beforeAll, describe, expect, it } from "vitest";
import type { FastifyInstance } from "fastify";
import type { DbHandle } from "../src/db/client.js";
import { readFileSync } from "node:fs";

import { makeFixtureRepo, makeTestApp } from "./helpers.js";
import { eq } from "drizzle-orm";
import { chatSessions, jargons, maiMessages } from "../src/db/schema.js";

describe("jargon 路由", () => {
  const repo = makeFixtureRepo();
  const { app, tokenManager, db } = makeTestApp({ repo });
  const token = tokenManager.getToken();
  const auth = { cookies: { maibot_session: token } };

  let server: FastifyInstance;
  let ids: Record<string, number> = {};

  beforeAll(() => {
    server = app;

    db.drizzle
      .insert(chatSessions)
      .values([
        { sessionId: "qq:group:111", platform: "qq", groupId: "111", groupName: "测试群", accountId: "bot1", userId: null },
        {
          sessionId: "qq:private:20001",
          platform: "qq",
          userId: "20001",
          userNickname: "小明",
          accountId: "bot1",
          groupId: null,
        },
      ])
      .run();
    // 旧聊天流：chat_sessions 里没有，靠最新消息兜底取名
    db.drizzle
      .insert(maiMessages)
      .values({
        messageId: "m1",
        timestamp: "2026-09-01 12:00:00",
        platform: "qq",
        userId: "30001",
        userNickname: "老用户",
        groupId: null,
        sessionId: "qq:legacy",
        rawContent: Buffer.from("x"),
        isMentioned: false,
        isAt: false,
        isEmoji: false,
        isPicture: false,
        isCommand: false,
        isNotify: false,
      })
      .run();

    const base = {
      evidenceMessages: null,
      lastInferenceCount: 0,
      createdTimestamp: "2026-09-01 10:00:00",
      updatedTimestamp: "2026-09-01 10:00:00",
    };
    const inserted = db.drizzle
      .insert(jargons)
      .values([
        {
          ...base,
          content: "绝绝子",
          meaning: "非常棒",
          sessionIdDict: JSON.stringify({ "qq:group:111": 5 }),
          count: 10,
          isJargon: 1,
          isComplete: true,
          isGlobal: false,
          createdBy: "AI",
        },
        {
          ...base,
          content: "yyds",
          meaning: "永远的神",
          sessionIdDict: JSON.stringify({ "qq:private:20001": 3, "qq:group:111": 2 }),
          count: 7,
          isJargon: 1,
          isComplete: false,
          isGlobal: false,
          createdBy: "AI",
        },
        {
          ...base,
          content: "老词条",
          meaning: "",
          sessionIdDict: "{}",
          count: 1,
          isJargon: 1,
          isComplete: false,
          isGlobal: false,
          createdBy: "AI",
        },
        {
          ...base,
          content: "手动词",
          meaning: "手动含义",
          sessionIdDict: JSON.stringify({ "qq:legacy": 1 }),
          count: 2,
          isJargon: 1,
          isComplete: true,
          isGlobal: false,
          createdBy: "MANUAL",
        },
      ])
      .returning({ id: jargons.id, content: jargons.content })
      .all();
    ids = Object.fromEntries(inserted.map((row) => [row.content, row.id]));
    ids = { ...ids };
  });

  afterAll(() => {
    db.close();
  });

  it("list：按 count 倒序 + 聊天名解析（chat_sessions 优先 / messages 兜底）", async () => {
    const res = await server.inject({ method: "GET", url: "/api/webui/jargon/list", ...auth });
    expect(res.statusCode).toBe(200);
    const body = res.json() as { success: boolean; total: number; data: Array<Record<string, unknown>> };
    expect(body.success).toBe(true);
    expect(body.total).toBe(4);
    expect(body.data.map((item) => item.content)).toEqual(["绝绝子", "yyds", "手动词", "老词条"]);

    const juanjuezi = body.data[0]!;
    expect(juanjuezi.chat_name).toBe("测试群");
    expect(juanjuezi.is_jargon).toBe(true);
    expect(juanjuezi.created_by).toBe("AI");

    const shoudong = body.data[2]!;
    // qq:legacy 不在 chat_sessions → 最新消息兜底：「老用户的私聊」
    expect(shoudong.chat_name).toBe("老用户的私聊");

    const legacy = body.data[3]!;
    // 历史遗留：标记为黑话但没有含义
    expect(legacy.is_legacy_empty_meaning).toBe(true);
    expect(legacy.is_jargon).toBe(false);
  });

  it("list：jargon_status 与 search 筛选", async () => {
    const confirmed = await server.inject({
      method: "GET",
      url: "/api/webui/jargon/list?jargon_status=confirmed_jargon",
      ...auth,
    });
    expect(confirmed.json().total).toBe(3); // 「老词条」是 legacy 空含义，不算有效黑话

    const notJargon = await server.inject({
      method: "GET",
      url: "/api/webui/jargon/list?jargon_status=confirmed_not_jargon",
      ...auth,
    });
    expect(notJargon.json().total).toBe(1);

    const searched = await server.inject({
      method: "GET",
      url: "/api/webui/jargon/list?search=%E7%BB%9D%E7%BB%9D%E5%AD%90",
      ...auth,
    });
    expect(searched.json().total).toBe(1);
    expect(searched.json().data[0].chat_names).toEqual(["测试群"]);
  });

  it("stats/summary：各状态计数与聊天分布", async () => {
    const res = await server.inject({ method: "GET", url: "/api/webui/jargon/stats/summary", ...auth });
    const data = res.json().data;
    expect(data.total).toBe(4);
    expect(data.confirmed_jargon).toBe(3);
    expect(data.confirmed_not_jargon).toBe(1);
    expect(data.manual_jargon).toBe(1);
    expect(data.chat_count).toBe(3);
    expect(data.top_chats["qq:group:111"]).toBe(2);
  });

  it("detail：命中与 404 形状", async () => {
    const ok = await server.inject({
      method: "GET",
      url: `/api/webui/jargon/${ids["绝绝子"]}`,
      ...auth,
    });
    expect(ok.statusCode).toBe(200);
    expect(ok.json().data.meaning).toBe("非常棒");

    const missing = await server.inject({ method: "GET", url: "/api/webui/jargon/99999", ...auth });
    expect(missing.statusCode).toBe(404);
    expect(missing.json()).toEqual({ detail: "黑话不存在" });
  });

  it("create：手动黑话创建；同范围同内容手动去重；替换同范围 AI 记录", async () => {
    // 目标聊天流必须真实存在
    const missing = await server.inject({
      method: "POST",
      url: "/api/webui/jargon",
      ...auth,
      payload: { content: "新词", meaning: "含义", session_ids: ["qq:ghost"] },
    });
    expect(missing.statusCode).toBe(400);
    expect(missing.json().detail).toBe("聊天流不存在: qq:ghost");

    // 替换同范围 AI 记录（「绝绝子」原为 AI、范围重叠）
    const created = await server.inject({
      method: "POST",
      url: "/api/webui/jargon",
      ...auth,
      payload: { content: "绝绝子", meaning: "手动含义", session_ids: ["qq:group:111", "qq:private:20001"], is_global: false },
    });
    expect(created.statusCode).toBe(200);
    const body = created.json() as { success: boolean; message: string; data: Record<string, unknown> };
    expect(body.message).toBe("创建成功");
    expect(body.data.created_by).toBe("MANUAL");
    expect(body.data.is_jargon).toBe(true);
    expect(body.data.chat_names).toEqual(["测试群", "小明的私聊"]);

    // 旧 AI 记录已被删除
    const oldGone = await server.inject({
      method: "GET",
      url: `/api/webui/jargon/${ids["绝绝子"]}`,
      ...auth,
    });
    expect(oldGone.statusCode).toBe(404);

    // 同范围同内容的手动黑话已存在 → 400
    const duplicated = await server.inject({
      method: "POST",
      url: "/api/webui/jargon",
      ...auth,
      payload: { content: "绝绝子", meaning: "再写一遍", session_ids: ["qq:group:111"] },
    });
    expect(duplicated.statusCode).toBe(400);
    expect(duplicated.json().detail).toBe("该范围中已存在相同内容的手动黑话");
  });

  it("patch：含义更新与空白内容 400", async () => {
    const patched = await server.inject({
      method: "PATCH",
      url: `/api/webui/jargon/${ids["yyds"]}`,
      ...auth,
      payload: { meaning: "新的含义" },
    });
    expect(patched.json()).toMatchObject({ success: true, message: "更新成功", data: { meaning: "新的含义" } });

    const blank = await server.inject({
      method: "PATCH",
      url: `/api/webui/jargon/${ids["yyds"]}`,
      ...auth,
      payload: { content: "   " },
    });
    expect(blank.statusCode).toBe(400);
    expect(blank.json()).toEqual({ detail: "黑话内容不能为空" });
  });

  it("batch/set-jargon 与批量删除", async () => {
    const setStatus = await server.inject({
      method: "POST",
      url: `/api/webui/jargon/batch/set-jargon?ids=${ids["yyds"]}&is_jargon=false`,
      ...auth,
    });
    expect(setStatus.json()).toEqual({ success: true, message: "成功更新 1 条黑话状态" });

    const batchDelete = await server.inject({
      method: "POST",
      url: "/api/webui/jargon/batch/delete",
      ...auth,
      payload: { ids: [ids["yyds"], ids["老词条"]] },
    });
    expect(batchDelete.json()).toEqual({ success: true, message: "成功删除 2 条黑话", deleted_count: 2 });

    const empty = await server.inject({
      method: "POST",
      url: "/api/webui/jargon/batch/delete",
      ...auth,
      payload: { ids: [] },
    });
    expect(empty.statusCode).toBe(400);
    expect(empty.json()).toEqual({ detail: "ID列表不能为空" });
  });

  it("export：ids 缺失 400 / include_chat_info 目标形状", async () => {
    const missing = await server.inject({
      method: "POST",
      url: "/api/webui/jargon/export",
      ...auth,
      payload: { ids: [ids["手动词"], 99999] },
    });
    expect(missing.statusCode).toBe(400);
    expect(missing.json().detail).toContain("部分黑话不存在");

    const exported = await server.inject({
      method: "POST",
      url: "/api/webui/jargon/export",
      ...auth,
      payload: { ids: [ids["手动词"]], include_chat_info: true },
    });
    const body = exported.json() as {
      type: string;
      version: number;
      count: number;
      jargons: Array<{ content: string; created_by: string; targets: Array<Record<string, unknown>> | null }>;
    };
    expect(body.type).toBe("maibot.jargon.export");
    expect(body.count).toBe(1);
    expect(body.jargons[0]!.created_by).toBe("MANUAL");
    // qq:legacy 不在 chat_sessions → targets 为空数组
    expect(body.jargons[0]!.targets).toEqual([]);
  });

  it("import：skip 跳过重叠、overwrite 覆盖；目标不存在 400", async () => {
    const ghostTargets = await server.inject({
      method: "POST",
      url: "/api/webui/jargon/import",
      ...auth,
      payload: {
        target_session_ids: ["qq:ghost"],
        jargons: [
          { content: "x", meaning: "", count: 0, is_jargon: true, is_complete: false, is_global: false, created_by: "MANUAL" },
        ],
      },
    });
    expect(ghostTargets.statusCode).toBe(400);

    // 「绝绝子」已在 qq:group:111 存在（手动）→ skip 策略跳过
    const skip = await server.inject({
      method: "POST",
      url: "/api/webui/jargon/import",
      ...auth,
      payload: {
        target_session_ids: ["qq:group:111", "qq:private:20001"],
        conflict_strategy: "skip",
        jargons: [
          { content: "绝绝子", meaning: "导入含义", count: 5, is_jargon: true, is_complete: false, is_global: false, created_by: "MANUAL" },
        ],
      },
    });
    expect(skip.json()).toEqual({
      success: true,
      message: "导入完成：成功 0 个，跳过 1 个，失败 0 个",
      imported_count: 0,
      skipped_count: 1,
      failed_count: 0,
    });

    // overwrite：删除重叠记录后导入
    const overwrite = await server.inject({
      method: "POST",
      url: "/api/webui/jargon/import",
      ...auth,
      payload: {
        target_session_ids: ["qq:group:111", "qq:private:20001"],
        conflict_strategy: "overwrite",
        jargons: [
          { content: "绝绝子", meaning: "导入含义", count: 5, is_jargon: true, is_complete: false, is_global: false, created_by: "MANUAL" },
        ],
      },
    });
    expect(overwrite.json()).toMatchObject({ success: true, imported_count: 1, skipped_count: 0 });

    const imported = db.drizzle.select().from(jargons).where(eqContent("绝绝子")).all();
    expect(imported).toHaveLength(1);
    expect(imported[0]!.createdBy).toBe("MANUAL");
    expect(JSON.parse(imported[0]!.sessionIdDict)).toEqual({ "qq:group:111": 5, "qq:private:20001": 5 });
  });
});

function eqContent(content: string) {
  return eq(jargons.content, content);
}
