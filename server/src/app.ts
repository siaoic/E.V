/**
 * Fastify 应用工厂（对照 src/webui/app.py 的 create_app）。
 *
 * 阶段①承载：cookie / CORS / 静态资源 / 默认鉴权守卫 / 认证与系统路由。
 * 阶段③在此注册 @fastify/websocket 统一网关；阶段②开始逐路由迁移
 * 其余 /api/webui 子路由。
 */

import { existsSync } from "node:fs";
import path from "node:path";

import fastifyCookie from "@fastify/cookie";
import fastifyCors from "@fastify/cors";
import fastifyStatic from "@fastify/static";
import Fastify, { type FastifyInstance } from "fastify";
import type { FastifyBaseLogger } from "fastify";
import type pino from "pino";

import type { DbHandle } from "./db/client.js";
import { LocalStore } from "./services/local-store.js";

import { WsTokenStore } from "./auth/ws-tokens.js";
import { CookiePolicy } from "./auth/cookies.js";
import type { TokenManager } from "./auth/token-manager.js";
import type { WebUiSettings } from "./config/loader.js";
import { registerApiGuard } from "./http/guard.js";
import { registerAuthRoutes } from "./http/routes/auth-routes.js";
import { registerConfigRoutes } from "./http/routes/config-routes.js";
import { registerJargonRoutes } from "./http/routes/jargon-routes.js";
import { registerPersonRoutes } from "./http/routes/person-routes.js";
import { registerStatisticsRoutes } from "./http/routes/statistics-routes.js";
import { registerSystemRoutes } from "./http/routes/system-routes.js";

export interface AppOptions {
  settings: WebUiSettings;
  tokenManager: TokenManager;
  logger: FastifyBaseLogger;
  /** 仓库根：静态资源（dashboard/dist）与 pyproject 读取的基准目录。 */
  rootDir: string;
  /** 数据库句柄（person 等数据路由的数据源）；不传则数据路由不注册。 */
  db?: DbHandle;
  /** 本地 KV 存储（统计缓存等）；缺省时读写 data/local_store.json。 */
  localStore?: LocalStore;
  /** 真机前端还在 Python 侧时允许完全关闭静态托管（测试/并行运行用）。 */
  serveDashboard?: boolean;
  /** 注入 WS 临时 token 存储（测试用）；缺省时自建。 */
  wsTokens?: WsTokenStore;
}

/** 与 Python CORS 白名单一致的本地开发端口（调研 §10）。 */
const CORS_LOCAL_PORTS = [5173, 7999];

function isAllowedOrigin(origin: string | undefined, port: number): boolean {
  if (origin === undefined || origin === "null") {
    return false;
  }
  try {
    const url = new URL(origin);
    const localHost = url.hostname === "localhost" || url.hostname === "127.0.0.1" || url.hostname === "::1";
    if (!localHost) {
      return false;
    }
    return CORS_LOCAL_PORTS.includes(url.port === "" ? 80 : Number(url.port)) || Number(url.port) === port;
  } catch {
    return false;
  }
}

export function buildApp(options: AppOptions): FastifyInstance {
  const { settings, tokenManager, logger, rootDir } = options;

  const app = Fastify({
    loggerInstance: logger,
    trustProxy: true, // X-Forwarded-Proto / X-Forwarded-For 需要（cookie secure 判定与限流取 IP）
  });

  void app.register(fastifyCookie);

  void app.register(fastifyCors, {
    origin: (origin, callback) => {
      if (origin === undefined || isAllowedOrigin(origin, settings.port)) {
        callback(null, true);
        return;
      }
      callback(null, false);
    },
    credentials: true,
  });

  const cookiePolicy = new CookiePolicy(settings, logger);
  const wsTokens = options.wsTokens ?? new WsTokenStore();

  // 顺序关键：守卫 hook 必须先于路由注册（Fastify 在路由注册时捕获作用域 hook）
  const serveDashboard = (options.serveDashboard ?? true) && existsSync(dashboardDistOf(rootDir));
  registerApiGuard(app, {
    tokenManager,
    spaIndexFile: serveDashboard ? "index.html" : undefined,
  });

  registerSystemRoutes(app, { settings, rootDir });
  registerConfigRoutes(app, { rootDir, logger });

  if (options.db) {
    registerPersonRoutes(app, options.db);
    registerJargonRoutes(app, options.db);
    registerStatisticsRoutes(app, {
      db: options.db,
      localStore: options.localStore ?? LocalStore.open(path.join(rootDir, "data", "local_store.json"), logger as pino.Logger),
    });
  }
  registerAuthRoutes(app, {
    tokenManager,
    cookiePolicy,
    wsTokens,
    logger,
  });

  if (serveDashboard) {
    void app.register(fastifyStatic, { root: dashboardDistOf(rootDir) });
  }

  return app;
}

function dashboardDistOf(rootDir: string): string {
  return path.join(rootDir, "dashboard", "dist");
}
