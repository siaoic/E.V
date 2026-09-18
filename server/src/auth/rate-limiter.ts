/**
 * 登录限流（对应 src/webui/core/rate_limiter.py 的核心语义）：
 * 同一客户端 IP 在 window 秒窗口内失败 max_failures 次后，封禁 block_seconds。
 * 进程内状态（单实例假设，见调研 R5）。
 */

export interface RateLimitOptions {
  maxFailures: number;
  windowSeconds: number;
  blockSeconds: number;
}

export interface FailureResult {
  blocked: boolean;
  /** 剩余可尝试次数（本次失败计入后）。 */
  remaining: number;
}

export class RateLimiter {
  private readonly failures = new Map<string, number[]>();
  private readonly blocked = new Map<string, number>();

  constructor(private readonly options: RateLimitOptions) {}

  /** 客户端 IP：优先取反代头（与 Python `_get_client_ip` 的反代语义一致）。 */
  static clientIp(request: { headers: Record<string, unknown>; ip?: string }): string {
    const forwarded = request.headers["x-forwarded-for"];
    if (typeof forwarded === "string" && forwarded.trim() !== "") {
      return forwarded.split(",")[0].trim();
    }
    const realIp = request.headers["x-real-ip"];
    if (typeof realIp === "string" && realIp.trim() !== "") {
      return realIp.trim();
    }
    return request.ip ?? "unknown";
  }

  isBlocked(ip: string): { blocked: boolean; retryAfterSeconds: number | null } {
    this.cleanupExpiredBlocks();
    const unblockAt = this.blocked.get(ip);
    if (unblockAt !== undefined) {
      if (Date.now() >= unblockAt) {
        this.blocked.delete(ip);
        this.failures.delete(ip);
        return { blocked: false, retryAfterSeconds: null };
      }
      return { blocked: true, retryAfterSeconds: Math.ceil((unblockAt - Date.now()) / 1000) };
    }
    return { blocked: false, retryAfterSeconds: null };
  }

  /** 记录一次失败；返回是否因此被封禁。 */
  recordFailure(ip: string): FailureResult {
    const now = Date.now();
    const cutoff = now - this.options.windowSeconds * 1000;
    const recent = (this.failures.get(ip) ?? []).filter((time) => time > cutoff);
    recent.push(now);
    this.failures.set(ip, recent);

    const remaining = Math.max(this.options.maxFailures - recent.length, 0);
    if (recent.length >= this.options.maxFailures) {
      this.blocked.set(ip, now + this.options.blockSeconds * 1000);
      return { blocked: true, remaining };
    }
    return { blocked: false, remaining };
  }

  resetFailures(ip: string): void {
    this.failures.delete(ip);
  }

  private cleanupExpiredBlocks(): void {
    const now = Date.now();
    for (const [ip, unblockAt] of this.blocked) {
      if (now >= unblockAt) {
        this.blocked.delete(ip);
        this.failures.delete(ip);
      }
    }
  }
}
