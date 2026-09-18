/**
 * 配置路由（对照 src/webui/routers/config.py 的第一批迁移）。
 *
 * 本批落地（读路径 + raw 写入）：
 * - GET  /config/bot|model         解析后的配置文档（smol-toml）
 * - GET  /config/bot/raw           原始 TOML 文本
 * - POST /config/bot/raw           保存原始 TOML（写入前做语法校验）
 *
 * 显式未迁移（501，不静默假装成功）：
 * - 结构化写入（POST /config/bot、/config/model、各 section 端点）：依赖
 *   pydantic 配置模型做数值强转与语义校验（`Config.from_dict`），该模型是
 *   Python 侧单一事实源——待阶段④的「配置 schema 内网服务」落地后迁移；
 * - GET  /schema/*：同上（JSON Schema 由 pydantic 模型生成）；
 * - /prompts/*、/prompt-generator/*、/tts-audio/*、/adapter-config*：各自的
 *   依赖服务（prompt_manager、LLM 生成、sounddevice、适配器路径）分属后续批次。
 *
 * 用户数据红线：写入不做全文件重写格式的破坏性变更；raw 模式字节级保存。
 */

import { existsSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";

import { parse as parseToml } from "smol-toml";
import { z } from "zod";
import type { FastifyInstance, FastifyReply } from "fastify";

import type { FastifyBaseLogger } from "fastify";

const NOT_MIGRATED_DETAIL =
  "结构化配置写入与 schema 服务依赖 Python 配置模型（单一事实源），将在阶段④的内网服务落地后迁移；当前请使用「原始 TOML」模式编辑";

export interface ConfigRouteDeps {
  rootDir: string;
  logger: FastifyBaseLogger;
}

function configPath(rootDir: string, fileName: string): string {
  return path.join(rootDir, "config", fileName);
}

function readRawOr404(reply: FastifyReply, filePath: string): string | null {
  if (!existsSync(filePath)) {
    void reply.code(404).send({ detail: "配置文件不存在" });
    return null;
  }
  return readFileSync(filePath, "utf8");
}

export function registerConfigRoutes(app: FastifyInstance, deps: ConfigRouteDeps): void {
  const { rootDir, logger } = deps;

  // ===== 读取（全量迁移） =====

  const readConfig = async (reply: FastifyReply, fileName: string) => {
    const filePath = configPath(rootDir, fileName);
    if (!existsSync(filePath)) {
      return reply.code(404).send({ detail: "配置文件不存在" });
    }
    try {
      const config = parseToml(readFileSync(filePath, "utf8"));
      return reply.code(200).send({ success: true, config });
    } catch (error) {
      logger.error({ error, filePath }, "解析配置文件失败");
      return reply.code(500).send({ detail: `读取配置文件失败: ${String(error)}` });
    }
  };

  app.get("/api/webui/config/bot", async (_request, reply) => readConfig(reply, "bot_config.toml"));
  app.get("/api/webui/config/model", async (_request, reply) => readConfig(reply, "model_config.toml"));

  app.get("/api/webui/config/bot/raw", async (_request, reply) => {
    const raw = readRawOr404(reply, configPath(rootDir, "bot_config.toml"));
    if (raw === null) {
      return;
    }
    return { success: true, content: raw };
  });

  // ===== raw 写入（语法校验 + 字节级保存） =====

  const rawBodySchema = z.object({ raw_content: z.string() });

  app.post("/api/webui/config/bot/raw", async (request, reply) => {
    const parsed = rawBodySchema.safeParse(request.body ?? {});
    if (!parsed.success) {
      return reply.code(422).send({ detail: "缺少 raw_content" });
    }
    try {
      parseToml(parsed.data.raw_content);
    } catch (error) {
      return reply.code(400).send({ detail: `TOML 格式错误: ${String(error)}` });
    }
    // 语义校验（pydantic Config）待阶段④内网服务补齐；Python 启动时对非法配置
    // 仍会硬失败并明确报错，不会静默采用
    const filePath = configPath(rootDir, "bot_config.toml");
    writeFileSync(filePath, parsed.data.raw_content, "utf8");
    logger.info("麦麦主程序配置已更新（原始模式）");
    return { success: true, message: "配置已保存" };
  });

  // ===== 结构化写入与 schema（501：待阶段④配置校验服务） =====

  const notMigrated = async (_request: unknown, reply: FastifyReply) => {
    return reply.code(501).send({ detail: NOT_MIGRATED_DETAIL });
  };

  app.post("/api/webui/config/bot", notMigrated);
  app.post("/api/webui/config/model", notMigrated);
  app.post("/api/webui/config/bot/section/:section_name", notMigrated);
  app.post("/api/webui/config/model/section/:section_name", notMigrated);
  app.post("/api/webui/config/model/versions", notMigrated);
  app.patch("/api/webui/config/model/versions/:version_id", notMigrated);
  app.delete("/api/webui/config/model/versions/:version_id", notMigrated);
  app.post("/api/webui/config/model/versions/:version_id/activate", notMigrated);
  app.get("/api/webui/config/schema/bot", notMigrated);
  app.get("/api/webui/config/schema/model", notMigrated);
  app.get("/api/webui/config/schema/section/:section_name", notMigrated);
  app.get("/api/webui/config/tts-audio-devices", notMigrated);
  app.post("/api/webui/config/tts-audio/upload", notMigrated);
  app.get("/api/webui/config/adapter-config", notMigrated);
  app.post("/api/webui/config/adapter-config", notMigrated);
  app.get("/api/webui/config/adapter-config/path", notMigrated);
  app.post("/api/webui/config/adapter-config/path", notMigrated);
  app.get("/api/webui/config/prompts", notMigrated);
  app.get("/api/webui/config/prompts/:language/:filename", notMigrated);
  app.put("/api/webui/config/prompts/:language/:filename", notMigrated);
  app.delete("/api/webui/config/prompts/:language/:filename", notMigrated);
  app.post("/api/webui/config/prompt-generator/generate", notMigrated);
  app.post("/api/webui/config/prompt-generator/apply", notMigrated);
  app.get("/api/webui/config/maisaka-prompt-preview", notMigrated);
}
