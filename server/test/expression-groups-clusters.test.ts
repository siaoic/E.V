// expression groups / clusters 收尾批次的对拍测试

import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";

import { makeFixtureRepo, makeTestApp } from "./helpers.js";
import { botPlatformAccounts, chatSessions, expressions } from "../src/db/schema.js";
import { sqliteDatetime } from "../src/db/datetime.js";

describe("expression groups / clusters", () => {
  const repo = makeFixtureRepo();
  const { app, tokenManager, db } = makeTestApp({ repo });
  const token = tokenManager.getToken();
  const auth = { cookies: { maibot_session: token } };

  beforeAll(() => {
    db.drizzle.insert(botPlatformAccounts).values({
      platform: "qq",
      accountId: "bot1",
      disabled: false,
      firstSeenAt: sqliteDatetime(),
      lastSeenAt: sqliteDatetime(),
      lastSource: "test",
    }).run();

    db.drizzle
      .insert(chatSessions)
      .values([
        { sessionId: "qq:group:111", platform: "qq", accountId: "bot1", groupId: "111", groupName: "测试群", userId: null },
        {
          sessionId: "qq:private:20001",
          platform: "qq",
          accountId: "bot1",
          userId: "20001",
          userNickname: "小明",
          groupId: null,
        },
      ])
      .run();

    for (const row of [
      {
        situation: "被夸奖时",
        style: "害羞地道谢",
        contentList: "[]",
        count: 5,
        lastActiveTime: sqliteDatetime(),
        createTime: sqliteDatetime(),
        sessionId: "qq:group:111",
        checked: false,
      },
      {
        situation: "被感谢时",
        style: "温暖回应",
        contentList: "[]",
        count: 3,
        lastActiveTime: sqliteDatetime(),
        createTime: sqliteDatetime(),
        sessionId: "qq:private:20001",
        checked: true,
        modifiedBy: "USER",
      },
    ]) {
      db.drizzle.insert(expressions).values(row).run();
    }

    // 配置：共享组（精确群 + 全通配）+ 表达向量索引路径与 payload
    const configDir = path.join(repo.rootDir, "config");
    const original = readFileSync(path.join(configDir, "bot_config.toml"), "utf8");
    const withGroups = `${original}
[expression]
expression_vector_index_path = "data/expression_vector_index.json"

[[expression.expression_groups]]
targets = [
  { platform = "qq", item_id = "111", type = "group" },
]

[[expression.expression_groups]]
targets = [
  { platform = "*", item_id = "*", type = "group" },
]
`;
    writeFileSync(path.join(configDir, "bot_config.toml"), withGroups, "utf8");

    const vectorDir = path.join(repo.rootDir, "data");
    mkdirSync(vectorDir, { recursive: true });
    writeFileSync(
      path.join(vectorDir, "expression_vector_index.json"),
      JSON.stringify({
        generated_at: "2026-09-19T00:00:00",
        embedding_model: "test-model",
        embedding_dimension: 8,
        sample_count: 2,
        clusters: [
          { embedding_profile_marker: "m1", cluster_id: 1, size: 2 },
          { embedding_profile_marker: "m1", cluster_id: 2, size: 1 },
        ],
        expressions: [
          { id: 11, cluster_id: 1, embedding_profile_marker: "m1", session_id: "qq:group:111", situation: "被夸奖时 ", style: "害羞地道谢", count: 5, checked: false, modified_by: "AI" },
          { id: 12, cluster_id: 1, embedding_profile_marker: "m1", session_id: "qq:private:20001", situation: "被感谢时", style: "温暖回应", count: 3, checked: true, modified_by: "USER" },
          { id: 13, cluster_id: 2, embedding_profile_marker: "m1", session_id: "qq:group:111", situation: "其它", style: "风格", count: 1, checked: false },
        ],
      }),
      "utf8",
    );
  });

  afterAll(() => {
    db.close();
  });

  it("groups：精确目标展开 + 全通配全局组 + 聊天信息含表达配置", async () => {
    const res = await app.inject({ method: "GET", url: "/api/webui/expression/groups", ...auth });
    expect(res.statusCode).toBe(200);
    const body = res.json() as {
      data: Array<{
        index: number;
        name: string;
        chat_ids: string[];
        members: Array<{ chat_id: string; use_expression: boolean; chat_name: string }>;
        is_global: boolean;
      }>;
    };
    expect(body.data).toHaveLength(2);
    // 组 1：精确匹配 qq 群 111
    expect(body.data[0]!.chat_ids).toEqual(["qq:group:111"]);
    expect(body.data[0]!.members[0]).toMatchObject({ chat_id: "qq:group:111", chat_name: "测试群", use_expression: true });
    // 组 2：全通配 → 全局组，包含全部可见聊天流
    expect(body.data[1]!.is_global).toBe(true);
    expect(new Set(body.data[1]!.chat_ids)).toEqual(new Set(["qq:group:111", "qq:private:20001"]));
  });

  it("clusters：索引摘要 + 成员按 count 倒序 + 404", async () => {
    const list = await app.inject({ method: "GET", url: "/api/webui/expression/clusters", ...auth });
    const listBody = list.json() as {
      index_exists: boolean;
      embedding_model: string | null;
      sample_count: number;
      clusters: Array<{ cluster_id: number; size: number; embedding_profile_marker: string }>;
    };
    expect(listBody.index_exists).toBe(true);
    expect(listBody.embedding_model).toBe("test-model");
    expect(listBody.sample_count).toBe(2);
    expect(listBody.clusters.map((cluster) => cluster.cluster_id)).toEqual([1, 2]); // size 倒序

    const members = await app.inject({
      method: "GET",
      url: "/api/webui/expression/clusters/1/members",
      ...auth,
    });
    const membersBody = members.json() as {
      cluster: { cluster_id: number };
      data: Array<{ id: number; chat_name: string | null; style: string; modified_by: string | null }>;
    };
    expect(membersBody.cluster.cluster_id).toBe(1);
    expect(membersBody.data.map((item) => item.id)).toEqual([11, 12]);
    // 成员名称经 normalize_text（首尾空白去除）
    expect(membersBody.data[0]!.style).toBe("害羞地道谢");
    expect(membersBody.data[0]!.chat_name).toBe("测试群");

    const missing = await app.inject({
      method: "GET",
      url: "/api/webui/expression/clusters/99/members",
      ...auth,
    });
    expect(missing.statusCode).toBe(404);
    expect(missing.json().detail).toBe("未找到表达聚类: 99");
  });
});
