// F4 契约：WS 一次性临时 token（60s、一次性、session 失效连带失效）

import { describe, expect, it } from "vitest";

import { WsTokenStore, WS_TOKEN_EXPIRE_SECONDS } from "../src/auth/ws-tokens.js";

describe("WsTokenStore", () => {
  it("有效期常量是 60 秒", () => {
    expect(WS_TOKEN_EXPIRE_SECONDS).toBe(60);
  });

  it("一次性消费：第一次 true，第二次 false", () => {
    const store = new WsTokenStore();
    const token = store.generate("session-token");
    expect(token.length).toBeGreaterThan(30);
    expect(store.consume(token, () => true)).toBe(true);
    expect(store.consume(token, () => true)).toBe(false);
  });

  it("关联 session 失效：消费失败且条目被删除", () => {
    const store = new WsTokenStore();
    const token = store.generate("dead-session");
    expect(store.consume(token, () => false)).toBe(false);
    expect(store.size).toBe(0);
  });

  it("过期 token：消费失败", () => {
    const store = new WsTokenStore();
    // 直接注入一条已过期记录（绕过 generate 的时钟）
    const expired = store.generate("session-token");
    // @ts-expect-error 测试专用：把过期时间改到过去
    store["entries"].set(expired, { expireAtMs: Date.now() - 1, sessionToken: "session-token" });
    expect(store.consume(expired, () => true)).toBe(false);
  });

  it("cleanup：generate 会顺手清理过期条目", () => {
    const store = new WsTokenStore();
    const stale = store.generate("s1");
    // @ts-expect-error 测试专用
    store["entries"].set(stale, { expireAtMs: Date.now() - 1000, sessionToken: "s1" });
    store.generate("s2");
    expect(store.size).toBe(1);
  });
});
