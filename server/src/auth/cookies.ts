/**
 * 认证 Cookie 语义（复刻 src/webui/core/auth.py，风险 R10）：
 * - Cookie 名 maibot_session，HttpOnly、samesite=lax、path=/、max_age 7 天；
 * - secure = 配置 secure_cookie 或 production 模式；但请求实际是 HTTP 时强制关闭
 *   （原实现的兼容行为，保持一致——包括那条 80 个等号的告警日志）；
 * - clear 与 set 不同：clear 不检查请求协议（原实现的差异，逐字保留）。
 */

import type { FastifyReply, FastifyRequest } from "fastify";
import type { FastifyBaseLogger } from "fastify";

import type { WebUiSettings } from "../config/loader.js";

export const COOKIE_NAME = "maibot_session";
export const COOKIE_MAX_AGE_SECONDS = 7 * 24 * 60 * 60;

export class CookiePolicy {
  constructor(
    private readonly settings: WebUiSettings,
    private readonly logger: FastifyBaseLogger,
  ) {}

  /** 对应 Python `_is_secure_environment()`（不含请求协议检测）。 */
  private baseSecure(): boolean {
    if (this.settings.secureCookie) {
      this.logger.info("配置中启用了 secure_cookie");
      return true;
    }
    if (this.settings.mode === "production") {
      this.logger.info("WebUI运行在生产模式，启用 secure cookie");
      return true;
    }
    return false;
  }

  /** 对应 `set_auth_cookie` 内的协议检测：HTTP 请求强制禁用 secure。 */
  effectiveSecure(request: FastifyRequest): boolean {
    let isSecure = this.baseSecure();
    const forwardedProto = String(request.headers["x-forwarded-proto"] ?? "").toLowerCase();
    const isHttps = forwardedProto !== "" ? forwardedProto === "https" : request.protocol === "https";
    if (!isHttps && isSecure) {
      this.logger.warn(
        "检测到 HTTP 连接但环境配置要求 HTTPS (secure cookie)——已自动禁用 secure 标志以允许登录。" +
          "如需启用：配置 webui.secure_cookie = true 并确保反代正确转发 X-Forwarded-Proto",
      );
      isSecure = false;
    }
    return isSecure;
  }

  setAuthCookie(reply: FastifyReply, token: string, request: FastifyRequest): void {
    const secure = this.effectiveSecure(request);
    void reply.setCookie(COOKIE_NAME, token, {
      maxAge: COOKIE_MAX_AGE_SECONDS,
      httpOnly: true,
      sameSite: "lax",
      secure,
      path: "/",
    });
    this.logger.info(
      `已设置认证 Cookie: ${token.slice(0, 8)}... (secure=${secure}, samesite=lax, httponly=True, path=/, max_age=${COOKIE_MAX_AGE_SECONDS})`,
    );
  }

  /** 与 set 不同：不做请求协议检测（原实现 `clear_auth_cookie` 的行为）。 */
  clearAuthCookie(reply: FastifyReply): void {
    const secure = this.baseSecure();
    void reply.clearCookie(COOKIE_NAME, {
      httpOnly: true,
      sameSite: secure ? "strict" : "lax",
      secure,
      path: "/",
    });
    this.logger.debug("已清除认证 Cookie");
  }
}
