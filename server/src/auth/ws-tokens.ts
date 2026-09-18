/**
 * WS 一次性临时 token（复刻 src/webui/routers/websocket/auth.py，调研 F4）：
 * - token_urlsafe(32) 生成，60 秒有效，一次性消费；
 * - 进程内字典存储（多实例时需换共享存储——见调研 §7.3 与 R5）；
 * - 消费时校验关联 session token 仍然有效，session 失效则一并删除。
 */

import { randomBytes } from "node:crypto";

export const WS_TOKEN_EXPIRE_SECONDS = 60;

interface TempTokenEntry {
  expireAtMs: number;
  sessionToken: string;
}

export class WsTokenStore {
  private readonly entries = new Map<string, TempTokenEntry>();

  private cleanupExpired(): void {
    const now = Date.now();
    for (const [token, entry] of this.entries) {
      if (now > entry.expireAtMs) {
        this.entries.delete(token);
      }
    }
  }

  /** token_urlsafe(32)：32 字节随机数的 base64url 形式。 */
  generate(sessionToken: string): string {
    this.cleanupExpired();
    const tempToken = randomBytes(32).toString("base64url");
    this.entries.set(tempToken, {
      expireAtMs: Date.now() + WS_TOKEN_EXPIRE_SECONDS * 1000,
      sessionToken,
    });
    return tempToken;
  }

  /**
   * 验证并消费；`isSessionValid` 注入 TokenManager.verifyToken，失败同样删除条目。
   */
  consume(
    tempToken: string,
    isSessionValid: (sessionToken: string) => boolean,
  ): boolean {
    this.cleanupExpired();
    const entry = this.entries.get(tempToken);
    if (entry === undefined) {
      return false;
    }
    if (Date.now() > entry.expireAtMs) {
      this.entries.delete(tempToken);
      return false;
    }
    if (!isSessionValid(entry.sessionToken)) {
      this.entries.delete(tempToken);
      return false;
    }
    // 一次性消费
    this.entries.delete(tempToken);
    return true;
  }

  /** 仅测试用。 */
  get size(): number {
    return this.entries.size;
  }
}
