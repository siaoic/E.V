/**
 * 系统路由（/api/webui 下，全部免鉴权，与现网一致）：
 * - GET /health                 → {"status":"healthy","service":"MaiBot WebUI"}
 * - GET /version-compatibility  → 版本四方组；版本格式非法或参数缺失时 422 {"detail": ...}
 */

import { z } from "zod";
import type { FastifyInstance } from "fastify";

import type { WebUiSettings } from "../../config/loader.js";
import { readProjectVersion, readRequiredWebuiVersion } from "../../config/loader.js";
import { getWebuiVersionCompatibility } from "../../versioning/compat.js";

export interface SystemRouteDeps {
  settings: WebUiSettings;
  /** 仓库根（测试时指向夹具目录）。 */
  rootDir: string;
}

const querySchema = z.object({
  webui_version: z.string().min(1).max(64),
});

export function registerSystemRoutes(app: FastifyInstance, deps: SystemRouteDeps): void {
  app.get("/api/webui/health", async () => ({ status: "healthy", service: "MaiBot WebUI" }));

  app.get("/api/webui/version-compatibility", async (request, reply) => {
    const parsed = querySchema.safeParse(request.query);
    if (!parsed.success) {
      return reply.code(422).send({ detail: "query.webui_version 必须是 1~64 位的版本号" });
    }
    try {
      const result = getWebuiVersionCompatibility(
        parsed.data.webui_version,
        readProjectVersion(deps.rootDir),
        readRequiredWebuiVersion(deps.rootDir),
      );
      return {
        status: result.status,
        main_program_version: result.mainProgramVersion,
        webui_version: result.webuiVersion,
        required_webui_version: result.requiredWebuiVersion,
      };
    } catch (error) {
      return reply.code(422).send({ detail: String(error instanceof Error ? error.message : error) });
    }
  });
}
