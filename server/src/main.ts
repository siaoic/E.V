/**
 * MaiBot TS 侧进程入口（对照 bot.py + src/main.py 的启动/退出语义）。
 *
 * - 退出码 42 = 「请求重启」：由外层监督循环捕获后原位重启（D3），
 *   与 bot.py 的 RESTART_EXIT_CODE 语义对齐；
 * - 数据库硬规则（R4）：MaiBot.db 只由本进程打开；Python 内核服务不得直连主库。
 */

import path from "node:path";

import pino from "pino";

import { LogRingBuffer } from "./logging/log-ring.js";
import type { FastifyBaseLogger } from "fastify";

import { REPO_ROOT, readWebUiSettings } from "./config/loader.js";
import { ensureRuntimePerformanceIndexes, ensureSchema, openDatabase } from "./db/client.js";
import { TokenManager } from "./auth/token-manager.js";
import { buildApp } from "./app.js";

const RESTART_EXIT_CODE = 42;

export const RESTART_REQUESTED = Symbol("restart-requested");

async function runOnce(rootDir: string, logger: FastifyBaseLogger): Promise<number | typeof RESTART_REQUESTED> {
  const settings = readWebUiSettings(rootDir);
  const tokenManager = TokenManager.open(path.join(rootDir, "data", "webui.json"), logger);

  const dbFile = process.env.MAIBOT_DB_FILE ?? path.join(rootDir, "data", "MaiBot.db");
  const db = openDatabase(dbFile, logger);
  ensureSchema(db, logger);
  ensureRuntimePerformanceIndexes(db, logger);

  const logBuffer = new LogRingBuffer(500);
  const app = await buildApp({
    settings,
    tokenManager,
    logger,
    rootDir,
    serveDashboard: process.env.MAIBOT_SERVE_DASHBOARD !== "false",
    logBuffer,
  });

  // runOnce 挂起到 finish() 被调用（关停/重启），保证监督循环语义正确
  let resolveDone!: (outcome: number | typeof RESTART_REQUESTED) => void;
  const done = new Promise<number | typeof RESTART_REQUESTED>((resolve) => {
    resolveDone = resolve;
  });

  let resolved = false;
  const finish = (outcome: number | typeof RESTART_REQUESTED) => {
    if (resolved) {
      return;
    }
    resolved = true;
    void app
      .close()
      .catch((error: unknown) => logger.error({ error }, "WebUI 关停异常"))
      .finally(() => {
        db.close();
        resolveDone(outcome);
      });
  };

  // 重启请求入口：阶段②的 system 路由（重启按钮）将调用它
  app.decorate("requestRestart", () => finish(RESTART_REQUESTED));

  const shutdown = (signal: string) => {
    logger.info({ signal }, "收到退出信号，正在关停");
    finish(0);
  };
  process.once("SIGINT", () => shutdown("SIGINT"));
  process.once("SIGTERM", () => shutdown("SIGTERM"));

  if (!settings.enabled) {
    logger.warn("[webui] enabled = false：WebUI 不启动（与 Python 侧行为一致）");
    finish(0);
    return done;
  }

  const bindHost = settings.host[0] ?? "127.0.0.1";
  try {
    await app.listen({ port: settings.port, host: bindHost });
    logger.info(
      `WebUI 已启动: http://${bindHost}:${settings.port}（mode=${settings.mode}；` +
        `多地址绑定见 TODO: host 列表的其余项 ${settings.host.slice(1).join(", ") || "无"}）`,
    );
    if (tokenManager.shouldShowStartupToken()) {
      logger.warn(`本次为临时 Token（未配置固定 Token），完整 Token: ${tokenManager.getToken()}`);
    }
  } catch (error) {
    logger.error({ error }, "WebUI 启动失败");
    finish(1);
  }
  return done;
}

async function main(): Promise<void> {
  const rootDir = process.env.MAIBOT_ROOT ?? REPO_ROOT;
  const logBuffer = new LogRingBuffer(500);
  const logger = pino(
    { name: "maibot-server", level: process.env.MAIBOT_LOG_LEVEL ?? "info" },
    pino.multistream([process.stdout, logBuffer.asStream()]),
  );
  logger.info({ rootDir }, "MaiBot TS 侧启动");

  // 外层监督循环：退出码 42 → 原位重启（D3）；其他退出码直接结束
  for (;;) {
    const outcome = await runOnce(rootDir, logger);
    if (outcome === RESTART_REQUESTED) {
      logger.warn("收到重启请求（退出码 42），正在重启…");
      continue;
    }
    process.exitCode = outcome;
    break;
  }
}

void main();
