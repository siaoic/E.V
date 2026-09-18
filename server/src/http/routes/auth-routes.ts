/**
 * 认证与 WS token 路由（/api/webui 下，全部免鉴权，与现网一致）。
 *
 * 响应形状与 FastAPI 版逐字对齐：
 * - POST /auth/verify   → TokenVerifyResponse / 429 {"detail": ...}
 * - GET  /auth/check    → {authenticated, ...}
 * - POST /auth/logout   → {"success": true, "message": "已成功登出"}
 * - POST /auth/update   → TokenUpdateResponse（需鉴权）
 * - GET  /ws-token      → 未认证时 HTTP 200 + success=false（刻意保留的怪癖，R10）
 */

import { z } from "zod";
import type { FastifyInstance } from "fastify";

import type { CookiePolicy } from "../../auth/cookies.js";
import { COOKIE_NAME } from "../../auth/cookies.js";
import { RateLimiter } from "../../auth/rate-limiter.js";
import {
  TOKEN_SOURCE_TEMPORARY,
  type TokenManager,
} from "../../auth/token-manager.js";
import type { WsTokenStore } from "../../auth/ws-tokens.js";
import type { FastifyBaseLogger } from "fastify";

export interface AuthRouteDeps {
  tokenManager: TokenManager;
  cookiePolicy: CookiePolicy;
  wsTokens: WsTokenStore;
  logger: FastifyBaseLogger;
}

const verifyBodySchema = z.object({ token: z.string() });
const updateBodySchema = z.object({ new_token: z.string() });

export function registerAuthRoutes(app: FastifyInstance, deps: AuthRouteDeps): void {
  const rateLimiter = new RateLimiter({ maxFailures: 5, windowSeconds: 300, blockSeconds: 600 });

  app.post("/api/webui/auth/verify", async (request, reply) => {
    const parsed = verifyBodySchema.safeParse(request.body);
    if (!parsed.success) {
      return reply.code(422).send({ detail: parsed.error.issues[0]?.message ?? "请求不合法" });
    }

    const ip = RateLimiter.clientIp(request);
    const { blocked } = rateLimiter.isBlocked(ip);
    if (blocked) {
      return reply.code(429).send({ detail: "认证失败次数过多，您的 IP 已被临时封禁 10 分钟" });
    }

    const { tokenManager, cookiePolicy } = deps;
    if (tokenManager.verifyToken(parsed.data.token)) {
      rateLimiter.resetFailures(ip);
      cookiePolicy.setAuthCookie(reply, parsed.data.token, request);
      return {
        valid: true,
        message: "Token 验证成功",
        is_first_setup: tokenManager.isFirstSetup(),
        token_source: tokenManager.getTokenSource(),
        requires_custom_token: tokenManager.getTokenSource() === TOKEN_SOURCE_TEMPORARY,
      };
    }

    const { blocked: nowBlocked, remaining } = rateLimiter.recordFailure(ip);
    if (nowBlocked) {
      return reply.code(429).send({ detail: "认证失败次数过多，您的 IP 已被临时封禁 10 分钟" });
    }
    let message = "Token 无效或已过期";
    if (remaining <= 2) {
      message += `（剩余 ${remaining} 次尝试机会）`;
    }
    return { valid: false, message };
  });

  app.get("/api/webui/auth/check", async (request) => {
    try {
      const cookieToken = request.cookies[COOKIE_NAME];
      if (typeof cookieToken !== "string" || !deps.tokenManager.verifyToken(cookieToken)) {
        return { authenticated: false };
      }
      const tokenSource = deps.tokenManager.getTokenSource();
      return {
        authenticated: true,
        token_source: tokenSource,
        requires_custom_token: tokenSource === TOKEN_SOURCE_TEMPORARY,
      };
    } catch {
      return { authenticated: false };
    }
  });

  app.post("/api/webui/auth/logout", async (_request, reply) => {
    deps.cookiePolicy.clearAuthCookie(reply);
    return { success: true, message: "已成功登出" };
  });

  app.post("/api/webui/auth/update", async (request, reply) => {
    const cookieToken = request.cookies[COOKIE_NAME];
    if (typeof cookieToken !== "string" || !deps.tokenManager.verifyToken(cookieToken)) {
      return reply.code(401).send({ detail: "Token 无效或已过期" });
    }
    const parsed = updateBodySchema.safeParse(request.body);
    if (!parsed.success) {
      return reply.code(422).send({ detail: parsed.error.issues[0]?.message ?? "请求不合法" });
    }
    const result = deps.tokenManager.updateToken(parsed.data.new_token);
    if (result.success) {
      // 更新成功后旧 Cookie 失效，要求重新登录（原实现如此）
      deps.cookiePolicy.clearAuthCookie(reply);
    }
    return { success: result.success, message: result.message };
  });

  app.get("/api/webui/ws-token", async (request, reply) => {
    const cookieToken = request.cookies[COOKIE_NAME];
    if (typeof cookieToken !== "string" || cookieToken === "") {
      // 200 + success=false：登录页的正常情况，避免前端因 401 刷新（原实现的刻意设计）
      deps.logger.debug("ws-token 请求：未提供认证信息（可能在登录页面）");
      return reply.send({
        success: false,
        message: "未提供认证信息，请先登录",
        token: null,
        expires_in: 0,
      });
    }
    if (!deps.tokenManager.verifyToken(cookieToken)) {
      deps.logger.debug("ws-token 请求：认证已过期");
      return reply.send({
        success: false,
        message: "认证已过期，请重新登录",
        token: null,
        expires_in: 0,
      });
    }
    const wsToken = deps.wsTokens.generate(cookieToken);
    return { success: true, token: wsToken, expires_in: WS_TOKEN_EXPIRES_IN };
  });
}

const WS_TOKEN_EXPIRES_IN = 60;
