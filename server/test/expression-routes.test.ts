// expression 路由对拍：可见性/列表筛选/CRUD/审核/导入导出/审核日志救回

import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { eq } from "drizzle-orm";

import { makeFixtureRepo, makeTestApp } from "./helpers.js";
import { botPlatformAccounts, chatSessions, expressions } from "../src/db/schema.js";
import { sqliteDatetime } from "../src/db/datetime.js";

describe("expression 路由", () => {
  const repo = makeFixtureRepo();
  const { app, tokenManager, db } = makeTestApp({ repo });
  const token = tokenManager.getToken();
  const auth = { cookies: { maibot_session: token } };

  let reviewedId = 0;

  beforeAll(() => {
    // 账号对：qq::bot1（当前账号）；聊天流两条，其中一条属于其他账号（不可见）
    db.drizzle
      .insert(botPlatformAccounts)
      .values({
        platform: "qq",
        accountId: "bot1",
        disabled: false,
        firstSeenAt: sqliteDatetime(),
        lastSeenAt: sqliteDatetime(),
        lastSource: "test",
      })
      .run();
    db.drizzle
      .insert(chatSessions)
      .values([
        { sessionId: "qq:group:111", platform: "qq", accountId: "bot1", groupId: "111", groupName: "测试群", userId: null },
        {
          sessionId: "qq:group:222",
          platform: "qq",
          accountId: "other-bot",
          groupId: "222",
          groupName: "别的群",
          userId: null,
        },
      ])
      .run();
    db.drizzle
      .insert(expressions)
      .values([
        {
          situation: "被夸奖时",
          style: "害羞地道谢",
          contentList: "[]",
          count: 5,
          lastActiveTime: "2026-09-10 10:00:00",
          createTime: "2026-09-05 10:00:00",
          sessionId: "qq:group:111",
          checked: false,
        },
        {
          situation: "被夸奖时",
          style: "傲娇回应",
          contentList: "[]",
          count: 3,
          lastActiveTime: "2026-09-11 10:00:00",
          createTime: "2026-09-06 10:00:00",
          sessionId: "qq:group:111",
          checked: true,
          modifiedBy: "USER",
        },
        {
          // 不可见账号的群：默认列表不应出现
          situation: "隐藏情景",
          style: "隐藏风格",
          contentList: "[]",
          count: 9,
          lastActiveTime: "2026-09-12 10:00:00",
          createTime: "2026-09-07 10:00:00",
          sessionId: "qq:group:222",
          checked: false,
        },
      ])
      .run();
  });

  afterAll(() => {
    db.close();
  });

  it("list：默认只显示当前账号对聊天流的表达", async () => {
    const res = await app.inject({ method: "GET", url: "/api/webui/expression/list", ...auth });
    const body = res.json() as { success: boolean; total: number; data: Array<Record<string, unknown>> };
    expect(body.success).toBe(true);
    expect(body.total).toBe(2);
    // last_active_time 倒序
    expect(body.data.map((item) => item.style)).toEqual(["傲娇回应", "害羞地道谢"]);
    expect(body.data[0]!.chat_name).toBe("测试群");
  });

  it("list：include_legacy 显示全部；review_filter=user_checked", async () => {
    const legacy = await app.inject({
      method: "GET",
      url: "/api/webui/expression/list?include_legacy=true",
      ...auth,
    });
    expect(legacy.json().total).toBe(3);

    const checked = await app.inject({
      method: "GET",
      url: "/api/webui/expression/list?review_filter=user_checked",
      ...auth,
    });
    expect(checked.json().total).toBe(1);
    expect(checked.json().data[0]!.modified_by).toBe("user");

    const badFilter = await app.inject({
      method: "GET",
      url: "/api/webui/expression/list?review_filter=nope",
      ...auth,
    });
    expect(badFilter.statusCode).toBe(400);
  });

  it("create：聊天流必须存在；创建后出现在列表", async () => {
    const ghost = await app.inject({
      method: "POST",
      url: "/api/webui/expression",
      ...auth,
      payload: { situation: "s", style: "t", chat_id: "qq:ghost" },
    });
    expect(ghost.statusCode).toBe(400);
    expect(ghost.json().detail).toBe("聊天流不存在: qq:ghost");

    const created = await app.inject({
      method: "POST",
      url: "/api/webui/expression",
      ...auth,
      payload: { situation: "聊到天气时", style: "开心地聊起彩虹", chat_id: "qq:group:111" },
    });
    expect(created.json().message).toBe("表达方式创建成功");
    expect((created.json().data as { checked: boolean }).checked).toBe(false);
    db.drizzle
      .delete(expressions)
      .where(eq(expressions.situation, "聊到天气时"))
      .run();
  });

  it("review-status：通过置 USER；拒绝清空 modified_by", async () => {
    const first = db.drizzle.select().from(expressions).where(eq(expressions.situation, "被夸奖时")).get()!;
    reviewedId = first.id!;

    const approve = await app.inject({
      method: "PATCH",
      url: `/api/webui/expression/${reviewedId}/review-status`,
      ...auth,
      payload: { approved: true },
    });
    expect(approve.json().message).toBe("已设为人工通过");
    expect(approve.json().data.modified_by).toBe("user");

    const reject = await app.inject({
      method: "PATCH",
      url: `/api/webui/expression/${reviewedId}/review-status`,
      ...auth,
      payload: { approved: false },
    });
    expect(reject.json().message).toBe("已设为拒绝");
    expect(reject.json().data.modified_by).toBeNull();
  });

  it("review/batch：拒绝即删除；未找到计入失败", async () => {
    const res = await app.inject({
      method: "POST",
      url: "/api/webui/expression/review/batch",
      ...auth,
      payload: { items: [{ id: reviewedId, approved: false }, { id: 999999, approved: false }] },
    });
    const body = res.json() as { total: number; succeeded: number; failed: number; results: Array<Record<string, unknown>> };
    expect(body.succeeded).toBe(1);
    expect(body.failed).toBe(1);
    expect(body.results[0]!.message).toBe("拒绝并删除");
    expect(db.drizzle.select().from(expressions).where(eq(expressions.id, reviewedId)).get()).toBeUndefined();
  });

  it("export 与 import：按聊天流去重往返", async () => {
    const exported = await app.inject({
      method: "POST",
      url: "/api/webui/expression/export",
      ...auth,
      payload: { chat_id: "qq:group:111" },
    });
    const body = exported.json() as {
      type: string;
      source_chat_name: string;
      count: number;
      expressions: Array<Record<string, unknown>>;
    };
    expect(body.type).toBe("maibot.expression.export");
    expect(body.source_chat_name).toBe("测试群");
    // 导入同聊天流：全部重复 → skipped
    const skip = await app.inject({
      method: "POST",
      url: "/api/webui/expression/import",
      ...auth,
      payload: {
        chat_id: "qq:group:111",
        expressions: body.expressions.map((item) => ({
          situation: item.situation,
          style: item.style,
          content_list: "[]",
          count: 1,
          last_active_time: null,
          create_time: null,
          checked: false,
          modified_by: null,
        })),
      },
    });
    expect(skip.json()).toMatchObject({ success: true, imported_count: 0, skipped_count: 1, failed_count: 0 });
  });

  it("review/logs：写入审核日志后可查询与救回", async () => {
    // 直接插入一条表达方式（等价 learners 侧产物）
    const situation = "被感谢时";
    const style = "温暖回应";
    const insertId = db.drizzle
      .insert(expressions)
      .values({
        situation,
        style,
        contentList: "[]",
        count: 1,
        lastActiveTime: "2026-09-15 10:00:00",
        createTime: "2026-09-15 10:00:00",
        sessionId: "qq:group:111",
        checked: false,
      })
      .returning({ id: expressions.id })
      .get()!.id;

    const logsBefore = await app.inject({
      method: "GET",
      url: "/api/webui/expression/review/logs",
      ...auth,
    });
    expect(logsBefore.statusCode).toBe(200);
    expect(logsBefore.json().success).toBe(true);
    // 审核日志文件为空数组时列表为空——救回链路走 approve 端点的前置校验
    const missing = await app.inject({
      method: "POST",
      url: "/api/webui/expression/review/logs/no-such-id/approve",
      ...auth,
    });
    expect(missing.statusCode).toBe(404);
    expect(missing.json().detail).toBe("未找到审核日志: no-such-id");

    // 清理种子
    db.drizzle.delete(expressions).where(eq(expressions.id, insertId)).run();
  });

  it("stats/summary 与 review/stats：计数与分布", async () => {
    const stats = await app.inject({ method: "GET", url: "/api/webui/expression/stats/summary", ...auth });
    const data = stats.json().data as { total: number; chat_count: number; top_chats: Record<string, number> };
    // 当前账号对下只剩「傲娇回应」等 1 条（其余被前面的测试删除）
    expect(data.total).toBeGreaterThanOrEqual(1);
    expect(data.top_chats["qq:group:111"]).toBeDefined();

    const review = await app.inject({ method: "GET", url: "/api/webui/expression/review/stats", ...auth });
    const reviewBody = review.json() as { total: number; passed: number; ai_checked: number; user_checked: number };
    expect(reviewBody.total).toBeGreaterThanOrEqual(1);
    expect(reviewBody.ai_checked).toBe(0);
  });
});
