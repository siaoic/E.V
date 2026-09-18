import { fileURLToPath } from 'node:url';
import { defineConfig } from 'vitest/config';

/** 开发期 `cortico/*` 指向平级的框架 checkout;与 tsconfig 的 `paths` 同步改。 */
const FRAMEWORK_SRC = fileURLToPath(new URL('../Cortico/src/', import.meta.url));

export default defineConfig({
  resolve: {
    alias: [{ find: /^cortico\//, replacement: FRAMEWORK_SRC }],
  },
  // 框架 checkout 在本包目录之外,要显式放进 vite 的可服务范围。
  server: {
    fs: { allow: [fileURLToPath(new URL('./', import.meta.url)), FRAMEWORK_SRC] },
  },
  test: {
    include: ['tests/**/*.test.ts'],
    env: { CORTICO_LANGUAGE: 'zh' },
    testTimeout: 20000,
    pool: 'forks',
  },
});
