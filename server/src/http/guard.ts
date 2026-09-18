/**
 * /api/webui/* 的默认鉴权守卫。
 *
 * Python 侧是「每个子路由显式声明 require_auth」；TS 侧改为默认拒绝 +
 * 豁免清单（与现网未鉴权端点逐一对齐），新增路由时漏加守卫不会裸奔——
 * 这是把 R1/R10 的防御方向从「列举要保护的」反转为「列举要放行的」。
 *
 * Fastify 语义注意：onRequest hook 必须在【根作用域】且【路由注册之前】
 * addHook 才能覆盖全部路由（封装作用域内的 hook 只影响作用域内注册的路由）。
 *
 * 未认证返回 FastAPI 同款形状：401 {"detail": "Token 无效或已过期"}。
 */

import type { FastifyInstance, FastifyRequest } from "fastify";

import type { TokenManager } from "../auth/token-manager.js";

/** 与现网 Python 路由逐一核对过的免鉴权端点（相对 /api/webui）。 */
export const PUBLIC_API_PATHS: ReadonlySet<string> = new Set([
  "/health",
  "/version-compatibility",
  "/auth/verify",
  "/auth/check",
  "/auth/logout",
  "/ws-token",
]);

const COOKIE_KEY = "maibot_session";
const API_PREFIX = "/api/webui";

export function isRequestAuthenticated(request: FastifyRequest, tokenManager: TokenManager): boolean {
  const cookieToken = request.cookies[COOKIE_KEY];
  return typeof cookieToken === "string" && tokenManager.verifyToken(cookieToken);
}

function requestApiPath(url: string): string {
  return url.slice(API_PREFIX.length).split("?")[0];
}

export interface GuardOptions {
  tokenManager: TokenManager;
  /** 非 /api 的 GET 落到该文件（SPA index.html）；不传则对未知路径统一 404。 */
  spaIndexFile?: string;
}

export function registerApiGuard(
  app: FastifyInstance,
  options: GuardOptions,
): void {
  const { tokenManager, spaIndexFile } = options;

  app.addHook("onRequest", async (request, reply) => {
    if (!request.url.startsWith(API_PREFIX)) {
      return;
    }
    const path = requestApiPath(request.url);
    if (PUBLIC_API_PATHS.has(path) || isRequestAuthenticated(request, tokenManager)) {
      return;
    }
    void reply.code(401).send({ detail: "Token 无效或已过期" });
  });

  app.setNotFoundHandler((request, reply) => {
    if (request.url.startsWith(API_PREFIX)) {
      const path = requestApiPath(request.url);
      if (!PUBLIC_API_PATHS.has(path) && !isRequestAuthenticated(request, tokenManager)) {
        void reply.code(401).send({ detail: "Token 无效或已过期" });
        return;
      }
      void reply.code(404).send({ detail: "Not Found" });
      return;
    }
    if (spaIndexFile !== undefined && request.method === "GET") {
      void reply.type("text/html").sendFile(spaIndexFile);
      return;
    }
    void reply.code(404).send({ detail: "Not Found" });
  });
}
