/**
 * Kernel 客户端层公共出口。
 * TS 侧唯一允许知道 Python 内核服务的模块层；HTTP 路由与 WS 网关
 * 禁止绕过此层直连 Python 端口（迁移方案 §1.1 kernel/ 层职责）。
 */

export { KernelClientError, KernelHttpClient, type KernelClientOptions, type KernelResponse } from "./http-client.js";
