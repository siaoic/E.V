/**
 * WebUI Token 管理（data/webui.json）。
 *
 * 与 Python 侧 src/webui/core/security.py 的 TokenManager 语义逐字对齐
 * （迁移调研 F3 / 风险 R10），包括这些「看上去像怪癖」的行为：
 * - token_source = "temporary" 时每次启动都重新生成 token（旧 Cookie 全部失效）；
 * - 单活动 token 模型：更新 token 即挤掉所有旧会话；
 * - regenerateToken 把 token_source 置为 "configured"（原实现如此）；
 * - verify 用常数时间比较（对应 secrets.compare_digest）。
 *
 * 文件格式（勿改，需与 Python 侧互换读写）：
 * { access_token, created_at, updated_at, first_setup_completed, token_source, setup_completed_at? }
 */

import { createHash, randomBytes, timingSafeEqual } from "node:crypto";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";

import type { FastifyBaseLogger } from "fastify";

export const TOKEN_SOURCE_TEMPORARY = "temporary";
export const TOKEN_SOURCE_CONFIGURED = "configured";
const VALID_TOKEN_SOURCES = new Set<string>([TOKEN_SOURCE_TEMPORARY, TOKEN_SOURCE_CONFIGURED]);

const SPECIAL_CHARS = "!@#$%^&*()_+-=[]{}|;:,.<>?/";

export interface WebUiStore {
  access_token: string;
  created_at: string;
  updated_at: string;
  first_setup_completed: boolean;
  token_source: string;
  setup_completed_at?: string;
}

/** 本地时间的 ISO 形式（对应 Python datetime.now().isoformat()，信息性字段）。 */
function localIsoNow(): string {
  const now = new Date();
  const pad = (value: number, width = 2) => String(value).padStart(width, "0");
  return (
    `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}` +
    `T${pad(now.getHours())}:${pad(now.getMinutes())}:${pad(now.getSeconds())}.${pad(now.getMilliseconds(), 3)}000`
  );
}

function timingSafeStringEqual(left: string, right: string): boolean {
  // 先哈希再比较：定长摘要保证 timingSafeEqual 不因长度不同抛错，同时保持常数时间语义
  const leftDigest = createHash("sha256").update(left, "utf8").digest();
  const rightDigest = createHash("sha256").update(right, "utf8").digest();
  return timingSafeEqual(leftDigest, rightDigest);
}

export function validateCustomToken(token: string): { valid: boolean; message: string } {
  if (token === "" || typeof token !== "string") {
    return { valid: false, message: "Token 不能为空" };
  }
  if (token.length < 10) {
    return { valid: false, message: "Token 长度至少为 10 位" };
  }
  if (!/[A-Z]/.test(token)) {
    return { valid: false, message: "Token 必须包含大写字母" };
  }
  if (!/[a-z]/.test(token)) {
    return { valid: false, message: "Token 必须包含小写字母" };
  }
  if (![...token].some((ch) => SPECIAL_CHARS.includes(ch))) {
    return { valid: false, message: `Token 必须包含特殊符号 (${SPECIAL_CHARS})` };
  }
  return { valid: true, message: "Token 格式正确" };
}

/** 旧版 64 位十六进制 token 校验（用于判定 token 来源）。 */
function isGeneratedTokenFormat(token: string): boolean {
  return /^[0-9a-fA-F]{64}$/.test(token);
}

export class TokenManager {
  private constructor(
    readonly storePath: string,
    private readonly logger: FastifyBaseLogger,
  ) {}

  static open(storePath: string, logger: FastifyBaseLogger): TokenManager {
    const manager = new TokenManager(storePath, logger);
    mkdirSync(path.dirname(storePath), { recursive: true });
    manager.ensureConfig();
    return manager;
  }

  private loadStore(): Partial<WebUiStore> {
    try {
      return JSON.parse(readFileSync(this.storePath, "utf8")) as Partial<WebUiStore>;
    } catch (error) {
      this.logger.error({ error }, "加载 WebUI 配置失败");
      return {};
    }
  }

  private saveStore(store: WebUiStore): void {
    writeFileSync(this.storePath, `${JSON.stringify(store, null, 2)}\n`, "utf8");
    this.logger.info({ path: this.storePath }, "WebUI 配置已保存");
  }

  resolveTokenSource(config: Partial<WebUiStore>): string {
    const configuredSource = String(config.token_source ?? "").trim().toLowerCase();
    if (VALID_TOKEN_SOURCES.has(configuredSource)) {
      return configuredSource;
    }
    const token = String(config.access_token ?? "");
    if (token === "") {
      return TOKEN_SOURCE_TEMPORARY;
    }
    // 旧版自定义 Token 不会是 64 位十六进制串，据此归为用户配置
    return isGeneratedTokenFormat(token) ? TOKEN_SOURCE_TEMPORARY : TOKEN_SOURCE_CONFIGURED;
  }

  ensureConfig(): void {
    if (!this.exists()) {
      this.logger.info({ path: this.storePath }, "WebUI 配置文件不存在，正在创建");
      this.createNewToken();
      return;
    }
    try {
      const config = this.loadStore();
      if (!config.access_token) {
        this.logger.warn("WebUI 配置文件中缺少 access_token，正在重新生成");
        this.createNewToken({ preserveSetupState: true });
        return;
      }
      const source = this.resolveTokenSource(config);
      if (config.token_source !== source) {
        this.saveStore({ ...(config as WebUiStore), token_source: source });
      }
      if (source === TOKEN_SOURCE_TEMPORARY) {
        this.logger.info("WebUI 尚未配置固定 Token，正在重新生成本次启动临时 Token");
        this.createNewToken({ preserveSetupState: true });
      } else {
        this.logger.info(`WebUI Token 已加载: ${String(config.access_token).slice(0, 8)}...`);
      }
    } catch (error) {
      this.logger.error({ error }, "读取 WebUI 配置文件失败，正在重新创建");
      this.createNewToken();
    }
  }

  private exists(): boolean {
    try {
      return readFileSync(this.storePath, "utf8").length >= 0;
    } catch {
      return false;
    }
  }

  private createNewToken(
    options: { tokenSource?: string; preserveSetupState?: boolean } = {},
  ): string {
    const { tokenSource = TOKEN_SOURCE_TEMPORARY, preserveSetupState = false } = options;
    const token = randomBytes(32).toString("hex");
    const existing = preserveSetupState && this.exists() ? this.loadStore() : {};
    const firstSetupCompleted = existing.first_setup_completed === true;

    const store: WebUiStore = {
      access_token: token,
      created_at: localIsoNow(),
      updated_at: localIsoNow(),
      first_setup_completed: firstSetupCompleted,
      token_source: VALID_TOKEN_SOURCES.has(tokenSource) ? tokenSource : TOKEN_SOURCE_TEMPORARY,
    };
    if (firstSetupCompleted && existing.setup_completed_at) {
      store.setup_completed_at = existing.setup_completed_at;
    }
    this.saveStore(store);
    this.logger.info(`新的 WebUI Token 已生成: ${token.slice(0, 8)}...`);
    return token;
  }

  getToken(): string {
    return this.loadStore().access_token ?? "";
  }

  verifyToken(token: string): boolean {
    if (token === "") {
      return false;
    }
    const currentToken = this.getToken();
    if (currentToken === "") {
      this.logger.error("系统中没有有效的 token");
      return false;
    }
    return timingSafeStringEqual(token, currentToken);
  }

  /** 更新为用户自定义 token；成功后旧会话全部失效（单活动 token 模型）。 */
  updateToken(newToken: string): { success: boolean; message: string } {
    const validation = validateCustomToken(newToken);
    if (!validation.valid) {
      this.logger.error(`Token 格式无效: ${validation.message}`);
      return { success: false, message: validation.message };
    }
    try {
      const config = this.loadStore();
      const oldToken = String(config.access_token ?? "").slice(0, 8);
      const store: WebUiStore = {
        ...(config as WebUiStore),
        access_token: newToken,
        updated_at: localIsoNow(),
        token_source: TOKEN_SOURCE_CONFIGURED,
      };
      this.saveStore(store);
      this.logger.info(`Token 已更新: ${oldToken}... -> ${newToken.slice(0, 8)}...`);
      return { success: true, message: "Token 更新成功" };
    } catch (error) {
      this.logger.error({ error }, "更新 Token 失败");
      return { success: false, message: `更新失败: ${String(error)}` };
    }
  }

  /** 重新生成 token（保留 first_setup_completed；原实现会把来源置为 configured）。 */
  regenerateToken(): string {
    this.logger.info("正在重新生成 WebUI Token...");
    const newToken = randomBytes(32).toString("hex");
    const config = this.loadStore();
    const oldToken = config.access_token ? config.access_token.slice(0, 8) : "无";
    const store: WebUiStore = {
      ...(config as WebUiStore),
      access_token: newToken,
      updated_at: localIsoNow(),
      first_setup_completed: config.first_setup_completed ?? true,
      token_source: TOKEN_SOURCE_CONFIGURED,
    };
    this.saveStore(store);
    this.logger.info(`WebUI Token 已重新生成: ${oldToken}... -> ${newToken.slice(0, 8)}...`);
    return newToken;
  }

  isFirstSetup(): boolean {
    return this.loadStore().first_setup_completed !== true;
  }

  getTokenSource(): string {
    return this.resolveTokenSource(this.loadStore());
  }

  shouldShowStartupToken(): boolean {
    return this.getTokenSource() === TOKEN_SOURCE_TEMPORARY;
  }

  markSetupCompleted(): boolean {
    try {
      const config = this.loadStore();
      const store: WebUiStore = {
        ...(config as WebUiStore),
        first_setup_completed: true,
        setup_completed_at: localIsoNow(),
      };
      this.saveStore(store);
      this.logger.info("首次配置已标记为完成");
      return true;
    } catch (error) {
      this.logger.error({ error }, "标记首次配置完成失败");
      return false;
    }
  }

  resetSetupStatus(): boolean {
    try {
      const config = this.loadStore();
      const { setup_completed_at: _removed, ...rest } = config as WebUiStore;
      this.saveStore({ ...rest, first_setup_completed: false });
      this.logger.info("首次配置状态已重置");
      return true;
    } catch (error) {
      this.logger.error({ error }, "重置首次配置状态失败");
      return false;
    }
  }
}
