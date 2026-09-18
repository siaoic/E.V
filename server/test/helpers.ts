// 测试夹具：临时仓库根 + TokenManager + Fastify 应用

import { mkdtempSync, mkdirSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";

import pino from "pino";

import { buildApp } from "../src/app.js";
import { TokenManager } from "../src/auth/token-manager.js";
import { WsTokenStore } from "../src/auth/ws-tokens.js";
import type { WebUiSettings } from "../src/config/loader.js";

export const silentLogger = pino({ level: "silent" });

/** light-my-request 的 set-cookie 可能是 string 或 string[]，规范化取第一个。 */
export function firstSetCookie(headers: Record<string, unknown>): string {
  const value = headers["set-cookie"];
  if (Array.isArray(value)) {
    return String(value[0] ?? "");
  }
  return String(value ?? "");
}

export interface FixtureRepo {
  rootDir: string;
  webuiJsonPath: string;
}

export function makeFixtureRepo(options?: { webui?: Partial<WebUiSettings> }): FixtureRepo {
  const rootDir = mkdtempSync(path.join(tmpdir(), "maibot-server-test-"));
  mkdirSync(path.join(rootDir, "config"), { recursive: true });
  mkdirSync(path.join(rootDir, "data"), { recursive: true });

  const webui = { enabled: true, port: 18081, mode: "development" as const, secureCookie: false, ...options?.webui };
  const toml = [
    "[webui]",
    `enabled = ${String(webui.enabled)}`,
    `port = ${webui.port}`,
    `mode = "${webui.mode}"`,
    `secure_cookie = ${String(webui.secureCookie)}`,
    "",
    "[project]",
    'version = "1.2.5"',
  ].join("\n");
  writeFileSync(path.join(rootDir, "config", "bot_config.toml"), toml, "utf8");
  writeFileSync(
    path.join(rootDir, "pyproject.toml"),
    [
      "[project]",
      'name = "maibot-test"',
      'version = "1.2.5"',
      'dependencies = ["fastapi>=0.116.0", "maibot-dashboard>=1.7.4"]',
      "",
    ].join("\n"),
    "utf8",
  );
  return { rootDir, webuiJsonPath: path.join(rootDir, "data", "webui.json") };
}

export interface TestAppOptions {
  repo: FixtureRepo;
  settings?: Partial<WebUiSettings>;
  wsTokens?: WsTokenStore;
  tokenManager?: TokenManager;
}

export function makeTestApp(options: TestAppOptions) {
  const settings: WebUiSettings = {
    enabled: true,
    host: ["127.0.0.1"],
    port: 18081,
    mode: "development",
    secureCookie: false,
    ...options.settings,
  };
  const tokenManager =
    options.tokenManager ?? TokenManager.open(options.repo.webuiJsonPath, silentLogger);
  const wsTokens = options.wsTokens ?? new WsTokenStore();
  const app = buildApp({
    settings,
    tokenManager,
    logger: silentLogger,
    rootDir: options.repo.rootDir,
    serveDashboard: false,
    wsTokens,
  });
  return { app, tokenManager, wsTokens };
}
