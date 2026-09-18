import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    include: ["test/**/*.test.ts"],
    // 每个测试文件独立进程，避免登录限流的进程内状态互相影响
    pool: "forks",
    testTimeout: 20000,
  },
});
