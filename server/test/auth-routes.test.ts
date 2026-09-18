// R10 集成契约：认证路由 + 默认守卫 + cookie 语义 + ws-token，响应形状与 FastAPI 版逐字对齐

import { describe, expect, it } from "vitest";

import { firstSetCookie, makeFixtureRepo, makeTestApp } from "./helpers.js";

describe("认证路由集成（development 模式）", () => {
  const repo = makeFixtureRepo();
  const { app, tokenManager, wsTokens: store } = makeTestApp({ repo });

  const correctToken = tokenManager.getToken();

  it("GET /health：形状逐字对齐", async () => {
    const res = await app.inject({ method: "GET", url: "/api/webui/health" });
    expect(res.statusCode).toBe(200);
    expect(res.json()).toEqual({ status: "healthy", service: "MaiBot WebUI" });
  });

  it("GET /version-compatibility：compatible / webui_outdated / main_program_outdated / 422", async () => {
    const ok = await app.inject({ method: "GET", url: "/api/webui/version-compatibility?webui_version=1.7.4" });
    expect(ok.statusCode).toBe(200);
    expect(ok.json()).toEqual({
      status: "compatible",
      main_program_version: "1.2.5",
      webui_version: "1.7.4",
      required_webui_version: "1.7.4",
    });

    const outdated = await app.inject({ method: "GET", url: "/api/webui/version-compatibility?webui_version=1.7.0" });
    expect(outdated.json().status).toBe("webui_outdated");

    const newer = await app.inject({ method: "GET", url: "/api/webui/version-compatibility?webui_version=1.8.0" });
    expect(newer.json().status).toBe("main_program_outdated");

    const bad = await app.inject({ method: "GET", url: "/api/webui/version-compatibility?webui_version=not-a-version" });
    expect(bad.statusCode).toBe(422);

    const missing = await app.inject({ method: "GET", url: "/api/webui/version-compatibility" });
    expect(missing.statusCode).toBe(422);
  });

  it("默认守卫：未认证访问未知端点 → 401 FastAPI 形状", async () => {
    const res = await app.inject({ method: "GET", url: "/api/webui/some-future-route" });
    expect(res.statusCode).toBe(401);
    expect(res.json()).toEqual({ detail: "Token 无效或已过期" });
  });

  it("auth/check 未认证 → {authenticated:false}", async () => {
    const res = await app.inject({ method: "GET", url: "/api/webui/auth/check" });
    expect(res.statusCode).toBe(200);
    expect(res.json()).toEqual({ authenticated: false });
  });

  it("auth/verify 正确 token：设置 HttpOnly/Lax/无 Secure 的 Cookie（HTTP 上强制关 secure 的怪癖）", async () => {
    const res = await app.inject({ method: "POST", url: "/api/webui/auth/verify", payload: { token: correctToken } });
    expect(res.statusCode).toBe(200);
    expect(res.json()).toEqual({
      valid: true,
      message: "Token 验证成功",
      is_first_setup: true,
      token_source: "temporary",
      requires_custom_token: true,
    });
    const cookie = firstSetCookie(res.headers);
    expect(cookie).toContain("maibot_session=");
    expect(cookie).toContain("HttpOnly");
    expect(cookie).toContain("SameSite=Lax");
    expect(cookie).toContain("Path=/");
    expect(cookie).toContain("Max-Age=604800");
    expect(cookie).not.toContain("Secure");

    const check = await app.inject({
      method: "GET",
      url: "/api/webui/auth/check",
      cookies: { maibot_session: correctToken },
    });
    expect(check.json()).toEqual({
      authenticated: true,
      token_source: "temporary",
      requires_custom_token: true,
    });

    // 带有效 Cookie 的守卫路由：404 而不是 401（FastAPI 形状）
    const unknown = await app.inject({
      method: "GET",
      url: "/api/webui/some-future-route",
      cookies: { maibot_session: correctToken },
    });
    expect(unknown.statusCode).toBe(404);
    expect(unknown.json()).toEqual({ detail: "Not Found" });
  });

  it("auth/verify 错误 token：第 3 次起带剩余次数提示（remaining<=2），第 5 次封禁 429，之后一律 429", async () => {
    const wrong = "0".repeat(64);
    for (let attempt = 1; attempt <= 2; attempt += 1) {
      const res = await app.inject({ method: "POST", url: "/api/webui/auth/verify", payload: { token: wrong } });
      expect(res.statusCode).toBe(200);
      expect(res.json()).toEqual({ valid: false, message: "Token 无效或已过期" });
    }
    const third = await app.inject({ method: "POST", url: "/api/webui/auth/verify", payload: { token: wrong } });
    expect(third.json()).toEqual({ valid: false, message: "Token 无效或已过期（剩余 2 次尝试机会）" });
    const fourth = await app.inject({ method: "POST", url: "/api/webui/auth/verify", payload: { token: wrong } });
    expect(fourth.json().message).toBe("Token 无效或已过期（剩余 1 次尝试机会）");

    const fifth = await app.inject({ method: "POST", url: "/api/webui/auth/verify", payload: { token: wrong } });
    expect(fifth.statusCode).toBe(429);
    expect(fifth.json()).toEqual({ detail: "认证失败次数过多，您的 IP 已被临时封禁 10 分钟" });

    const sixth = await app.inject({ method: "POST", url: "/api/webui/auth/verify", payload: { token: wrong } });
    expect(sixth.statusCode).toBe(429);

    // 正确 token 也被拦（封禁按 IP）
    const correctDuringBlock = await app.inject({
      method: "POST",
      url: "/api/webui/auth/verify",
      payload: { token: correctToken },
    });
    expect(correctDuringBlock.statusCode).toBe(429);
  });

  it("GET /ws-token：无 Cookie → 200 + success=false（刻意保留的怪癖）；带 Cookie → 成功且一次性", async () => {
    const unauth = await app.inject({ method: "GET", url: "/api/webui/ws-token" });
    expect(unauth.statusCode).toBe(200);
    expect(unauth.json()).toEqual({
      success: false,
      message: "未提供认证信息，请先登录",
      token: null,
      expires_in: 0,
    });

    const authed = await app.inject({
      method: "GET",
      url: "/api/webui/ws-token",
      cookies: { maibot_session: correctToken },
    });
    expect(authed.statusCode).toBe(200);
    const body = authed.json() as { success: boolean; token: string; expires_in: number };
    expect(body.success).toBe(true);
    expect(body.expires_in).toBe(60);

    // 与 Python 一致：临时 token 与当前 session 绑定，session 有效才可消费，且只能用一次
    expect(store.consume(body.token, (session) => session === correctToken)).toBe(true);
    expect(store.consume(body.token, () => true)).toBe(false);
  });

  it("auth/logout：清除 Cookie 并返回固定形状", async () => {
    const res = await app.inject({ method: "POST", url: "/api/webui/auth/logout" });
    expect(res.statusCode).toBe(200);
    expect(res.json()).toEqual({ success: true, message: "已成功登出" });
    expect(firstSetCookie(res.headers)).toContain("maibot_session=;");
  });

  it("auth/update：需要 Cookie；成功后清 Cookie 且换发 token", async () => {
    const noAuth = await app.inject({ method: "POST", url: "/api/webui/auth/update", payload: { new_token: "Whatever!123" } });
    expect(noAuth.statusCode).toBe(401);

    const bad = await app.inject({
      method: "POST",
      url: "/api/webui/auth/update",
      cookies: { maibot_session: correctToken },
      payload: { new_token: "short" },
    });
    expect(bad.statusCode).toBe(200);
    expect(bad.json()).toEqual({ success: false, message: "Token 长度至少为 10 位" });

    const good = await app.inject({
      method: "POST",
      url: "/api/webui/auth/update",
      cookies: { maibot_session: correctToken },
      payload: { new_token: "BrandNew!2026" },
    });
    expect(good.json()).toEqual({ success: true, message: "Token 更新成功" });
    expect(tokenManager.getToken()).toBe("BrandNew!2026");
    // 旧 token 立即失效（单活动 token 模型）
    expect(tokenManager.verifyToken(correctToken)).toBe(false);
    expect(firstSetCookie(good.headers)).toContain("maibot_session=;");
  });
});

describe("认证路由集成（production 模式：HTTP 上强制关 Secure 的怪癖 + 反代 HTTPS 时启用）", () => {
  it("production + 纯 HTTP：Cookie 仍不带 Secure（原实现兼容行为）", async () => {
    const repo = makeFixtureRepo({ webui: { mode: "production", secureCookie: false } });
    const { app, tokenManager } = makeTestApp({ repo, settings: { mode: "production" } });
    const res = await app.inject({
      method: "POST",
      url: "/api/webui/auth/verify",
      payload: { token: tokenManager.getToken() },
    });
    expect(res.statusCode).toBe(200);
    expect(firstSetCookie(res.headers)).not.toContain("Secure");
  });

  it("production + X-Forwarded-Proto: https：Cookie 带 Secure", async () => {
    const repo = makeFixtureRepo({ webui: { mode: "production", secureCookie: false } });
    const { app, tokenManager } = makeTestApp({ repo, settings: { mode: "production" } });
    const res = await app.inject({
      method: "POST",
      url: "/api/webui/auth/verify",
      payload: { token: tokenManager.getToken() },
      headers: { "x-forwarded-proto": "https" },
    });
    expect(firstSetCookie(res.headers)).toContain("Secure");
  });

  it("secure_cookie = true + HTTP：同样被强制关闭并告警", async () => {
    const repo = makeFixtureRepo({ webui: { mode: "development", secureCookie: true } });
    const { app, tokenManager } = makeTestApp({ repo, settings: { secureCookie: true } });
    const res = await app.inject({
      method: "POST",
      url: "/api/webui/auth/verify",
      payload: { token: tokenManager.getToken() },
    });
    expect(firstSetCookie(res.headers)).not.toContain("Secure");
  });
});
