// config 路由第一批：读路径全量 + raw 写入（语法校验）+ 未迁移端点显式 501

import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { readFileSync, writeFileSync } from "node:fs";
import path from "node:path";

import { makeFixtureRepo, makeTestApp } from "./helpers.js";

describe("config 路由（第一批）", () => {
  const repo = makeFixtureRepo();
  const { app, tokenManager, db } = makeTestApp({ repo });
  const token = tokenManager.getToken();
  const auth = { cookies: { maibot_session: token } };

  const botConfigPath = path.join(repo.rootDir, "config", "bot_config.toml");
  const original = readFileSync(botConfigPath, "utf8");

  beforeAll(() => {
    // fixture 的 bot_config 里补一个带行内注释的键，验证 raw 写回保留行为
    writeFileSync(botConfigPath, `${original}\n[chat]\nmax_context = 20 # 上下文条数。\n`, "utf8");
  });

  afterAll(() => {
    db.close();
  });

  it("GET /config/bot：解析后的配置文档", async () => {
    const res = await app.inject({ method: "GET", url: "/api/webui/config/bot", ...auth });
    if (res.statusCode !== 200) {
      console.log("DEBUG status", res.statusCode, res.body);
    }
    expect(res.statusCode).toBe(200);
    const body = res.json() as { success: boolean; config: { webui: { port: number }; chat: { max_context: number } } };
    expect(body.success).toBe(true);
    expect(body.config.webui.port).toBe(18081);
    expect(body.config.chat.max_context).toBe(20);
  });

  it("GET /config/bot/raw：原始文本", async () => {
    const res = await app.inject({ method: "GET", url: "/api/webui/config/bot/raw", ...auth });
    expect(res.statusCode).toBe(200);
    const body = res.json() as { success: boolean; content: string };
    expect(body.success).toBe(true);
    expect(body.content).toContain("max_context = 20 # 上下文条数。");
  });

  it("POST /config/bot/raw：合法 TOML 保存且字节级保留注释", async () => {
    const current = readFileSync(botConfigPath, "utf8");
    const updated = current.replace("max_context = 20 # 上下文条数。", "max_context = 32 # 上下文条数。");
    const res = await app.inject({
      method: "POST",
      url: "/api/webui/config/bot/raw",
      ...auth,
      payload: { raw_content: updated },
    });
    expect(res.json()).toEqual({ success: true, message: "配置已保存" });
    expect(readFileSync(botConfigPath, "utf8")).toContain("max_context = 32 # 上下文条数。");
  });

  it("POST /config/bot/raw：非法 TOML → 400 且不落盘", async () => {
    const before = readFileSync(botConfigPath, "utf8");
    const res = await app.inject({
      method: "POST",
      url: "/api/webui/config/bot/raw",
      ...auth,
      payload: { raw_content: "[broken\nport = ===" },
    });
    expect(res.statusCode).toBe(400);
    expect(res.json().detail).toContain("TOML 格式错误");
    expect(readFileSync(botConfigPath, "utf8")).toBe(before);
  });

  it("结构化写入与 schema：显式 501（待阶段④配置校验服务）", async () => {
    const post = await app.inject({ method: "POST", url: "/api/webui/config/bot", ...auth, payload: {} });
    expect(post.statusCode).toBe(501);
    expect(post.json().detail).toContain("阶段④");

    const schema = await app.inject({ method: "GET", url: "/api/webui/config/schema/bot", ...auth });
    expect(schema.statusCode).toBe(501);

    const section = await app.inject({
      method: "POST",
      url: "/api/webui/config/bot/section/chat",
      ...auth,
      payload: {},
    });
    expect(section.statusCode).toBe(501);
  });
});
