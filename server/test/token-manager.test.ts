// R10 契约测试：TokenManager 语义与 Python security.py 逐字对齐

import { describe, expect, it } from "vitest";

import { readFileSync, writeFileSync, existsSync } from "node:fs";
import path from "node:path";

import { silentLogger, makeFixtureRepo } from "./helpers.js";
import { TokenManager } from "../src/auth/token-manager.js";

describe("TokenManager", () => {
  it("首次创建：写入完整字段，token 为 64 位十六进制，来源 temporary", () => {
    const repo = makeFixtureRepo();
    const manager = TokenManager.open(repo.webuiJsonPath, silentLogger);

    const raw = JSON.parse(readFileSync(repo.webuiJsonPath, "utf8")) as Record<string, unknown>;
    expect(Object.keys(raw).sort()).toEqual(
      ["access_token", "created_at", "first_setup_completed", "token_source", "updated_at"].sort(),
    );
    expect(raw.access_token).toMatch(/^[0-9a-f]{64}$/);
    expect(raw.token_source).toBe("temporary");
    expect(raw.first_setup_completed).toBe(false);
    expect(manager.isFirstSetup()).toBe(true);
  });

  it("temporary 来源：重新 open 会换 token（旧 Cookie 全失效的怪癖，R10）", () => {
    const repo = makeFixtureRepo();
    const first = TokenManager.open(repo.webuiJsonPath, silentLogger);
    const oldToken = first.getToken();

    const second = TokenManager.open(repo.webuiJsonPath, silentLogger);
    expect(second.getToken()).not.toBe(oldToken);
    expect(second.verifyToken(oldToken)).toBe(false);
  });

  it("configured 来源（用户自定义 token）：重新 open 保留原 token", () => {
    const repo = makeFixtureRepo();
    const path2 = repo.webuiJsonPath;
    writeFileSync(
      path2,
      JSON.stringify({
        access_token: "MyCustom-Token!abc",
        created_at: "x",
        updated_at: "x",
        first_setup_completed: true,
        token_source: "configured",
      }),
      "utf8",
    );
    const manager = TokenManager.open(path2, silentLogger);
    expect(manager.getToken()).toBe("MyCustom-Token!abc");
    expect(manager.getTokenSource()).toBe("configured");
  });

  it("旧版无 token_source 字段：非 64hex 视为 configured，64hex 视为 temporary", () => {
    const repo = makeFixtureRepo();
    writeFileSync(
      repo.webuiJsonPath,
      JSON.stringify({ access_token: "legacy-custom!", first_setup_completed: true }),
      "utf8",
    );
    const configured = TokenManager.open(repo.webuiJsonPath, silentLogger);
    expect(configured.getTokenSource()).toBe("configured");
    expect(configured.getToken()).toBe("legacy-custom!");

    writeFileSync(
      repo.webuiJsonPath,
      JSON.stringify({ access_token: "a".repeat(64), first_setup_completed: true }),
      "utf8",
    );
    const temporary = TokenManager.open(repo.webuiJsonPath, silentLogger);
    // temporary → 本次启动重新生成
    expect(temporary.getToken()).not.toBe("a".repeat(64));
  });

  it("verify：空 token 与错误 token 都无效", () => {
    const repo = makeFixtureRepo();
    const manager = TokenManager.open(repo.webuiJsonPath, silentLogger);
    expect(manager.verifyToken("")).toBe(false);
    expect(manager.verifyToken("x".repeat(64))).toBe(false);
    expect(manager.verifyToken(manager.getToken())).toBe(true);
  });

  it("updateToken：格式校验逐条对齐 Python 文案", () => {
    const repo = makeFixtureRepo();
    const manager = TokenManager.open(repo.webuiJsonPath, silentLogger);

    expect(manager.updateToken("")).toEqual({ success: false, message: "Token 不能为空" });
    expect(manager.updateToken("aB1!x")).toEqual({ success: false, message: "Token 长度至少为 10 位" });
    expect(manager.updateToken("abcd1234!@#")).toEqual({ success: false, message: "Token 必须包含大写字母" });
    expect(manager.updateToken("ABCD1234!@#")).toEqual({ success: false, message: "Token 必须包含小写字母" });
    expect(manager.updateToken("aBcd12345678")).toEqual({
      success: false,
      message: "Token 必须包含特殊符号 (!@#$%^&*()_+-=[]{}|;:,.<>?/)",
    });

    const result = manager.updateToken("NewToken!2026");
    expect(result.success).toBe(true);
    expect(manager.getToken()).toBe("NewToken!2026");
    expect(manager.getTokenSource()).toBe("configured");
    expect(manager.verifyToken("NewToken!2026")).toBe(true);
  });

  it("regenerateToken：保留 first_setup_completed，并把来源置为 configured（原实现如此）", () => {
    const repo = makeFixtureRepo();
    const manager = TokenManager.open(repo.webuiJsonPath, silentLogger);
    manager.markSetupCompleted();
    const oldToken = manager.getToken();

    manager.regenerateToken();
    expect(manager.getToken()).not.toBe(oldToken);
    expect(manager.isFirstSetup()).toBe(false);
    expect(manager.getTokenSource()).toBe("configured");

    const raw = JSON.parse(readFileSync(repo.webuiJsonPath, "utf8")) as Record<string, unknown>;
    expect(typeof raw.setup_completed_at).toBe("string");
  });

  it("resetSetupStatus：清掉 setup_completed_at 并回到首次配置态", () => {
    const repo = makeFixtureRepo();
    const manager = TokenManager.open(repo.webuiJsonPath, silentLogger);
    manager.markSetupCompleted();
    expect(manager.resetSetupStatus()).toBe(true);

    const raw = JSON.parse(readFileSync(repo.webuiJsonPath, "utf8")) as Record<string, unknown>;
    expect("setup_completed_at" in raw).toBe(false);
    expect(raw.first_setup_completed).toBe(false);
    expect(existsSync(path.join(repo.rootDir, "data", "webui.json"))).toBe(true);
  });
});
