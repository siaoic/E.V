import { access, readdir, readFile } from 'node:fs/promises';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import type { Context } from '@deepseek-ai/cordis';
import { EnvError, substituteEnv } from './config.ts';

export type PluginFailure = { file: string; reason: string };
export type LoadedPlugin = { file: string; fiber: Awaited<ReturnType<Context['plugin']>> };

export type ImportFn = (url: string) => Promise<unknown>;

const DEFAULT_IMPORT: ImportFn = (url) => import(url);

// 扫描插件目录，两种形式都收：
// 1. 文件夹包（推荐）：<name>/package.json 的 main 字段指入口（默认 index.ts）；
//    停用 = 文件夹改名加 .disabled 后缀
// 2. 单文件：*.ts；忽略 _ 开头、*.disabled.ts、*.test.ts
// 名称排序 = 加载顺序（00-19 服务 provider、20-79 消费者/观察者、80+ 事件生产者）
export async function scanPlugins(dir: string): Promise<{ file: string; fullPath: string }[]> {
  const entries = await readdir(dir, { withFileTypes: true });
  const found: { file: string; fullPath: string }[] = [];
  for (const entry of entries) {
    if (entry.isDirectory()) {
      if (entry.name.startsWith('_') || entry.name.endsWith('.disabled')) continue;
      const folder = path.join(dir, entry.name);
      let main = 'index.ts';
      try {
        const pkg = JSON.parse(await readFile(path.join(folder, 'package.json'), 'utf8')) as { main?: unknown };
        if (typeof pkg.main === 'string' && pkg.main.trim()) main = pkg.main.trim();
      } catch {
        // 没有 package.json 时按默认 index.ts
      }
      const fullPath = path.normalize(path.join(folder, main));
      try {
        await access(fullPath);
      } catch {
        continue;
      }
      found.push({ file: path.posix.normalize(`${entry.name}/${main.split('\\').join('/')}`), fullPath });
    } else if (entry.isFile() && entry.name.endsWith('.ts')) {
      if (entry.name.startsWith('_')) continue;
      if (entry.name.endsWith('.disabled.ts') || entry.name.endsWith('.test.ts')) continue;
      found.push({ file: entry.name, fullPath: path.join(dir, entry.name) });
    }
  }
  return found.sort((a, b) => a.file.localeCompare(b.file));
}

export function resolvePlugin(mod: unknown, file: string): { apply: (...args: unknown[]) => unknown; config: unknown } {
  const candidate = mod as { apply?: unknown; default?: unknown; config?: unknown };
  const apply = typeof candidate?.apply === 'function' ? candidate.apply : candidate?.default;
  if (typeof apply !== 'function') throw new Error('没有 apply 或 default 导出');
  void file;
  return { apply: apply as (...args: unknown[]) => unknown, config: candidate?.config };
}

export async function loadPlugins(
  root: Context,
  pluginsDir: string,
  importModule: ImportFn = DEFAULT_IMPORT,
): Promise<{ fibers: LoadedPlugin[]; failures: PluginFailure[] }> {
  const entries = await scanPlugins(pluginsDir);
  const fibers: LoadedPlugin[] = [];
  const failures: PluginFailure[] = [];
  for (const entry of entries) {
    try {
      const mod = await importModule(pathToFileURL(entry.fullPath).href);
      const { apply, config } = resolvePlugin(mod, entry.file);
      const resolved = substituteEnv(config ?? {}, process.env);
      const fiber = await root.plugin(apply as never, resolved as never);
      fibers.push({ file: entry.file, fiber });
    } catch (error) {
      const reason = error instanceof EnvError ? error.message : error instanceof Error ? error.message : String(error);
      failures.push({ file: entry.file, reason });
    }
  }
  return { fibers, failures };
}
