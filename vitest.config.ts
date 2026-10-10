import { defineConfig } from 'vitest/config';

export default defineConfig({
  test: {
    include: [
      'src/viewer/**/*.test.ts',
      'src/loader/**/*.test.ts',
      'src/agent/**/*.test.ts',
      'src/llm/**/*.test.ts',
      'src/memory/**/*.test.ts',
      'src/games/**/*.test.ts',
      'plugins/**/*.test.ts',
    ],
    exclude: ['**/node_modules/**', '**/.git/**', 'src/loader/fixtures/**'],
  },
});
