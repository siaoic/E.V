import path from 'node:path';
import { Context } from '@deepseek-ai/cordis';
import { describe, expect, it } from 'vitest';
import { loadPlugins, resolvePlugin, scanPlugins } from './load.ts';

const fixturesDir = path.join(import.meta.dirname, 'fixtures');

async function collect(root: Context, event = 'plugin-loaded'): Promise<unknown[]> {
  const seen: unknown[] = [];
  root.on(event as never, ((value: unknown) => {
    seen.push(value);
  }) as never);
  return seen;
}

describe('scanPlugins', () => {
  it('文件夹包取 package.json 的 main 入口，无 package.json 用默认 index.ts；单文件收 .ts；忽略 _ 前缀与 .disabled；按名称排序', async () => {
    const files = await scanPlugins(fixturesDir);
    expect(files.map((entry) => entry.file)).toEqual([
      '01-one/index.ts',
      '02-two/index.ts',
      'custom-main/src/entry.ts',
      'hello.ts',
    ]);
  });

  it('目录不存在时抛错', async () => {
    await expect(scanPlugins(path.join(fixturesDir, 'nope'))).rejects.toThrow();
  });
});

describe('resolvePlugin', () => {
  it('取 apply 导出与 config', () => {
    const apply = () => {};
    const { apply: gotApply, config } = resolvePlugin({ apply, config: { a: 1 } }, 'x.ts');
    expect(gotApply).toBe(apply);
    expect(config).toEqual({ a: 1 });
  });

  it('apply 缺失时回退 default，两者都没有则抛错', () => {
    const fn = () => {};
    expect(resolvePlugin({ default: fn }, 'x.ts').apply).toBe(fn);
    expect(() => resolvePlugin({ config: {} }, 'x.ts')).toThrow(/apply/);
  });
});

describe('loadPlugins', () => {
  it('注入 importModule 时验证 config 的 env 替换与传递', async () => {
    const root = new Context();
    const received: unknown[] = [];
    const module = {
      apply: (_ctx: unknown, config: unknown) => {
        received.push(config);
      },
      config: { token: '${T_TOKEN}', port: '${T_PORT:-8787}' },
    };
    const previous = process.env.T_TOKEN;
    process.env.T_TOKEN = 'abc';
    try {
      const modules = [module];
      // 目录里有 3 个文件：第一个吃掉注入的模块，其余用无行为插件垫底
      const importFn = async () => modules.shift() ?? { apply: () => {}, config: {} };
      const { failures } = await loadPlugins(root, fixturesDir, importFn);
      expect(failures).toEqual([]);
      expect(received).toEqual([{ token: 'abc', port: '8787' }]);
    } finally {
      if (previous === undefined) delete process.env.T_TOKEN;
      else process.env.T_TOKEN = previous;
    }
  });

  it('缺环境变量的插件报错跳过，其余继续加载', async () => {
    const root = new Context();
    const seen = await collect(root);
    const bad = { apply: () => {}, config: { token: '${T_MISSING_XYZ}' } };
    const good = { apply: (ctx: { emit(name: string, value: unknown): void }) => ctx.emit('plugin-loaded', 'good'), config: {} };
    const noop = { apply: () => {}, config: {} };
    const modules = [bad, good, noop, { apply: () => {}, config: {} }];
    const importFn = async () => modules.shift();

    const { fibers, failures } = await loadPlugins(root, fixturesDir, importFn);

    expect(failures).toEqual([{ file: expect.any(String), reason: expect.stringContaining('T_MISSING_XYZ') }]);
    await vi_wait();
    expect(seen).toEqual(['good']);
    expect(fibers).toHaveLength(3);
  });

  it('没有 apply 导出的坏模块跳过，provider 抛错也不拖垮其他插件', async () => {
    const root = new Context();
    const seen = await collect(root);
    const broken = { config: {} };
    const throwing = { apply: () => { throw new Error('boom'); }, config: {} };
    const good = { apply: (ctx: { emit(name: string, value: unknown): void }) => ctx.emit('plugin-loaded', 'good'), config: {} };
    const modules = [broken, throwing, good, { apply: () => {}, config: {} }];
    const { failures } = await loadPlugins(root, fixturesDir, async () => modules.shift());

    expect(failures.map((failure) => failure.reason)).toEqual([expect.stringContaining('apply'), 'boom']);
    await vi_wait();
    expect(seen).toEqual(['good']);
  });

  it('真实动态 import：文件夹包与单文件插件都生效', async () => {
    const root = new Context();
    const seen = await collect(root);
    const { failures } = await loadPlugins(root, fixturesDir);

    expect(failures).toEqual([]);
    await vi_wait();
    expect(seen).toEqual(['one', 'two', 'custom', 'hello']);
  });
});

async function vi_wait() {
  await new Promise((resolve) => setImmediate(resolve));
}
