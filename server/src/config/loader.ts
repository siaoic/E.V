/**
 * 配置读取：config/bot_config.toml 与 pyproject.toml。
 *
 * 阶段①只读 [webui] 节与项目版本（鉴权与启动需要）；完整的配置升级
 * （CONFIG_VERSION 机制）属阶段②，届时必须遵守 R12：TOML 注释保留式
 * 手术写入，禁止整文件重写。
 */

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";

import { parse as parseToml } from "smol-toml";

/** 仓库根目录（server/ 的上一级）。 */
export const REPO_ROOT = path.resolve(fileURLToPath(import.meta.url), "../../../..");

export interface WebUiSettings {
  enabled: boolean;
  /** 绑定地址列表，默认 ["127.0.0.1", "::1"]（与 Python 侧 WebUIConfig 相同）。 */
  host: string[];
  port: number;
  /** production 下默认启用 secure cookie（Python `_is_secure_environment` 语义）。 */
  mode: "development" | "production";
  secureCookie: boolean;
}

function readToml(filePath: string): Record<string, unknown> {
  // 解析失败必须抛出（启动失败并明确报错），绝不静默回退默认值——
  // 配置损坏时静默降级会用错误端口/模式起服务（迁移风险 R10 的反面教材）
  return parseToml(readFileSync(filePath, "utf8")) as Record<string, unknown>;
}

function section(root: Record<string, unknown>, name: string): Record<string, unknown> {
  const value = root[name];
  return typeof value === "object" && value !== null ? (value as Record<string, unknown>) : {};
}

export function readWebUiSettings(rootDir: string = REPO_ROOT): WebUiSettings {
  const config = readToml(path.join(rootDir, "config", "bot_config.toml"));
  const webui = section(config, "webui");
  return {
    enabled: webui.enabled !== false,
    host: Array.isArray(webui.host) && webui.host.length > 0 ? (webui.host as string[]) : ["127.0.0.1", "::1"],
    port: typeof webui.port === "number" ? webui.port : 8001,
    mode: webui.mode === "development" ? "development" : "production",
    secureCookie: webui.secure_cookie === true,
  };
}

/** 主程序版本：pyproject.toml 的 project.version（version-compatibility 的「主程序版本」源）。 */
export function readProjectVersion(rootDir: string = REPO_ROOT): string {
  const pyproject = readToml(path.join(rootDir, "pyproject.toml"));
  const project = section(pyproject, "project");
  const version = project.version;
  if (typeof version !== "string" || version.trim() === "") {
    throw new Error("pyproject.toml 缺少 project.version，无法确定主程序版本");
  }
  return version.trim();
}

/** 主程序要求的 WebUI 版本：pyproject 依赖里 maibot-dashboard >= / == 的版本。 */
export function readRequiredWebuiVersion(rootDir: string = REPO_ROOT): string {
  const pyproject = readToml(path.join(rootDir, "pyproject.toml"));
  const project = section(pyproject, "project");
  const dependencies = Array.isArray(project.dependencies) ? project.dependencies : [];
  const pattern = /^maibot-dashboard\s*(?:>=|==)\s*([^,;\s]+)/i;
  for (const dependency of dependencies) {
    if (typeof dependency !== "string") {
      continue;
    }
    const match = pattern.exec(dependency.trim());
    if (match !== null) {
      return match[1];
    }
  }
  throw new Error("pyproject.toml 未声明有效的 maibot-dashboard 版本约束（支持 >= 或 ==）");
}
