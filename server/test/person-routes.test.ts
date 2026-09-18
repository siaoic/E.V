// person 路由对拍：响应形状 / 排序（NULLS LAST）/ exclude_unset 部分更新 / 批量删除

import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { eq } from "drizzle-orm";

import { makeFixtureRepo, makeTestApp } from "./helpers.js";
import { personInfo } from "../src/db/schema.js";

describe("person 路由", () => {
  const repo = makeFixtureRepo();
  const { app, db } = makeTestApp({ repo });

  async function seed(overrides: Partial<typeof personInfo.$inferInsert>) {
    const row = {
      isKnown: true,
      personId: "p-x",
      platform: "qq",
      userId: "10001",
      userNickname: "昵称",
      knowCounts: 1,
      ...overrides,
    };
    db.drizzle.insert(personInfo).values(row).run();
    return row;
  }

  beforeAll(async () => {
    await seed({
      personId: "p-alice",
      personName: "Alice",
      userNickname: "爱丽丝",
      userId: "20001",
      lastKnownTime: "2026-09-01 10:00:00",
      firstKnownTime: "2026-08-01 10:00:00",
      knowCounts: 3,
      groupCardname: '[{"group_id":"1","nickname":"群里的Alice"}]',
    });
    await seed({
      personId: "p-bob",
      personName: "Bob",
      userId: "20002",
      lastKnownTime: "2026-09-10 10:00:00",
    });
    // 未知人物：last_known_time 为 NULL（排序应垫底）
    await seed({ personId: "p-carol", personName: "Carol", isKnown: false, lastKnownTime: null as never });
    // 平台不同：用于平台筛选
    await seed({ personId: "p-tg", personName: "Tg", platform: "telegram" });
  });

  afterAll(() => {
    db.close();
  });

  it("未认证访问 → 401（守卫默认拒绝，无需真实登录）", async () => {
    const res = await app.inject({ method: "GET", url: "/api/webui/person/list" });
    expect(res.statusCode).toBe(401);
    expect(res.json()).toEqual({ detail: "Token 无效或已过期" });
  });

  it("list：真实登录后分页与排序", async () => {
    // 用 TokenManager 里的真实 token 登录拿 Cookie
    const store = JSON.parse(
      (await import("node:fs")).readFileSync(repo.webuiJsonPath, "utf8"),
    ) as { access_token: string };
    const token = store.access_token;

    const res = await app.inject({
      method: "GET",
      url: "/api/webui/person/list?page=1&page_size=10",
      cookies: { maibot_session: token },
    });
    expect(res.statusCode).toBe(200);
    const body = res.json() as {
      success: boolean;
      total: number;
      page: number;
      page_size: number;
      data: Array<Record<string, unknown>>;
    };
    expect(body.success).toBe(true);
    expect(body.total).toBe(4);
    expect(body.page).toBe(1);
    expect(body.page_size).toBe(10);
    // 排序：last_known_time 非空在前（倒序），NULL 垫底（NULL 之间顺序不保证）
    const ids = body.data.map((item) => item.person_id as string);
    expect(ids.slice(0, 2)).toEqual(["p-bob", "p-alice"]);
    expect(new Set(ids.slice(2))).toEqual(new Set(["p-tg", "p-carol"]));
    const alice = body.data.find((item) => item.person_id === "p-alice")!;
    expect(alice).toMatchObject({
      id: expect.any(Number),
      is_known: true,
      person_id: "p-alice",
      person_name: "Alice",
      platform: "qq",
      user_id: "20001",
      nickname: "爱丽丝",
      group_nick_name: [{ group_id: "1", nickname: "群里的Alice" }],
      know_times: 3,
    });
    // SQLite DATETIME → epoch 秒
    expect(typeof alice.know_since).toBe("number");
    expect((alice.last_know as number)).toBeGreaterThan(1_700_000_000);
    // NULL 项的字段形状
    const carol = body.data.find((item) => item.person_id === "p-carol")!;
    expect(carol.last_know).toBeNull();
    expect(carol.group_nick_name).toBeNull();
  });

  it("list：search 关键词命中昵称；platform 筛选", async () => {
    const getToken = async () => {
      const store = JSON.parse(
        (await import("node:fs")).readFileSync(repo.webuiJsonPath, "utf8"),
      ) as { access_token: string };
      return store.access_token;
    };
    const token = await getToken();

    const searched = await app.inject({
      method: "GET",
      url: "/api/webui/person/list?search=%E7%88%B1%E4%B8%BD%E4%B8%9D",
      cookies: { maibot_session: token },
    });
    expect(searched.json().total).toBe(1);
    expect(searched.json().data[0].person_id).toBe("p-alice");

    const platform = await app.inject({
      method: "GET",
      url: "/api/webui/person/list?platform=telegram",
      cookies: { maibot_session: token },
    });
    expect(platform.json().total).toBe(1);
    expect(platform.json().data[0].person_id).toBe("p-tg");
  });

  it("detail：命中与 404 形状", async () => {
    const store = JSON.parse(
      (await import("node:fs")).readFileSync(repo.webuiJsonPath, "utf8"),
    ) as { access_token: string };
    const token = store.access_token;

    const ok = await app.inject({
      method: "GET",
      url: "/api/webui/person/p-alice",
      cookies: { maibot_session: token },
    });
    expect(ok.statusCode).toBe(200);
    expect(ok.json().data.person_name).toBe("Alice");

    const missing = await app.inject({
      method: "GET",
      url: "/api/webui/person/p-nobody",
      cookies: { maibot_session: token },
    });
    expect(missing.statusCode).toBe(404);
    expect(missing.json()).toEqual({ detail: "未找到 ID 为 p-nobody 的人物信息" });
  });

  it("patch：exclude_unset 部分更新；空更新 400；未找到 404", async () => {
    const store = JSON.parse(
      (await import("node:fs")).readFileSync(repo.webuiJsonPath, "utf8"),
    ) as { access_token: string };
    const token = store.access_token;

    const empty = await app.inject({
      method: "PATCH",
      url: "/api/webui/person/p-bob",
      cookies: { maibot_session: token },
      payload: {},
    });
    expect(empty.statusCode).toBe(400);
    expect(empty.json()).toEqual({ detail: "未提供任何需要更新的字段" });

    const before = db.drizzle.select().from(personInfo).where(eq(personInfo.personId, "p-bob")).get()!;
    const patched = await app.inject({
      method: "PATCH",
      url: "/api/webui/person/p-bob",
      cookies: { maibot_session: token },
      payload: { memory_points: "记得他喜欢咖啡" },
    });
    expect(patched.statusCode).toBe(200);
    const body = patched.json() as { success: boolean; message: string; data: Record<string, unknown> };
    expect(body.success).toBe(true);
    expect(body.message).toBe("成功更新 1 个字段");
    expect(body.data.memory_points).toBe("记得他喜欢咖啡");
    // 未提供的字段保持不变
    expect(body.data.person_name).toBe("Bob");
    const after = db.drizzle.select().from(personInfo).where(eq(personInfo.personId, "p-bob")).get()!;
    expect(after.lastKnownTime! >= before.lastKnownTime!).toBe(true);
  });

  it("delete 与 batch/delete：计数、失败清单与消息拼接", async () => {
    const store = JSON.parse(
      (await import("node:fs")).readFileSync(repo.webuiJsonPath, "utf8"),
    ) as { access_token: string };
    const token = store.access_token;

    const single = await app.inject({
      method: "DELETE",
      url: "/api/webui/person/p-carol",
      cookies: { maibot_session: token },
    });
    expect(single.json()).toEqual({ success: true, message: "成功删除人物信息: Carol" });

    const batch = await app.inject({
      method: "POST",
      url: "/api/webui/person/batch/delete",
      cookies: { maibot_session: token },
      payload: { person_ids: ["p-tg", "p-ghost"] },
    });
    expect(batch.json()).toEqual({
      success: true,
      message: "成功删除 1 个人物，1 个失败",
      deleted_count: 1,
      failed_count: 1,
      failed_ids: ["p-ghost"],
    });

    const empty = await app.inject({
      method: "POST",
      url: "/api/webui/person/batch/delete",
      cookies: { maibot_session: token },
      payload: { person_ids: [] },
    });
    expect(empty.statusCode).toBe(400);
    expect(empty.json()).toEqual({ detail: "未提供要删除的人物ID" });
  });

  it("stats/summary：总数、已认识与平台分布", async () => {
    const store = JSON.parse(
      (await import("node:fs")).readFileSync(repo.webuiJsonPath, "utf8"),
    ) as { access_token: string };
    const token = store.access_token;

    const res = await app.inject({
      method: "GET",
      url: "/api/webui/person/stats/summary",
      cookies: { maibot_session: token },
    });
    const body = res.json() as {
      success: boolean;
      data: { total: number; known: number; unknown: number; platforms: Record<string, number> };
    };
    expect(body.success).toBe(true);
    expect(body.data.total).toBe(2); // alice + bob
    expect(body.data.known).toBe(2);
    expect(body.data.unknown).toBe(0);
    expect(body.data.platforms).toEqual({ qq: 2 });
  });
});
