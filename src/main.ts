// 运行时入口：游戏与 llm 是核心，在 src/ 里静态装配；plugins/ 目录放可热插的部分。
// 插件 = 文件夹包（对齐 Cortico extensions 风格）：<name>/package.json 的 main 指入口（默认 index.ts），
// 实现文件全部住在文件夹里，自包含。停用 = 文件夹改名加 .disabled；也兼容单文件 .ts 插件。
// 文件名排序 = 加载顺序：00-19 服务 provider、20-79 消费者/观察者、80+ 事件生产者（弹幕源）。
import { Context, Logger } from '@deepseek-ai/cordis';
import type { Exporter } from '@deepseek-ai/cordis';
import { apply as llmClientApply, config as llmClientConfig } from './llm/client-plugin.ts';
import { apply as llmApply } from './llm/plugin.ts';
import { apply as sudokuApply, config as sudokuConfig } from './games/sudoku/plugin.ts';
import { apply as memoryApply, config as memoryConfig } from './memory/plugin.ts';
import { substituteEnv } from './loader/config.ts';
import { loadPlugins } from './loader/load.ts';

const pluginsDir = process.argv[2] ?? 'plugins';

const root = new Context();
const consoleExporter: Exporter = {
  colors: 0,
  // 2 = LoggerLevel.WARN；const enum 被 Node 类型擦除，运行时无此导出
  levels: { default: 2 },
  export(message) {
    console.log(`[log:${message.name}:${message.type}] ${Logger.format(consoleExporter, message)}`);
  },
};
root.logger.exporter(consoleExporter);

type Fiber = Awaited<ReturnType<Context['plugin']>>;
type PluginFailure = { file: string; reason: string };
const fibers: { file: string; fiber: Fiber }[] = [];
const failures: PluginFailure[] = [];

// 核心静态装配（llm client / llm / memory / sudoku），env 替换与失败隔离行为和目录插件一致
const staticWiring = [
  { file: 'llm-client', start: () => root.plugin(llmClientApply, substituteEnv(llmClientConfig, process.env) as never) },
  { file: 'llm', start: () => root.plugin(llmApply, {}) },
  { file: 'memory', start: () => root.plugin(memoryApply, substituteEnv(memoryConfig, process.env) as never) },
  { file: 'sudoku', start: () => root.plugin(sudokuApply, substituteEnv(sudokuConfig, process.env) as never) },
];
for (const wiring of staticWiring) {
  try {
    fibers.push({ file: wiring.file, fiber: await wiring.start() });
  } catch (error) {
    failures.push({ file: wiring.file, reason: error instanceof Error ? error.message : String(error) });
  }
}

const loaded = await loadPlugins(root, pluginsDir);
fibers.push(...loaded.fibers);
failures.push(...loaded.failures);
for (const failure of failures) console.error(`[loader] ${failure.file}: ${failure.reason}`);
if (fibers.length === 0) {
  console.error('[loader] 没有可加载的插件，退出');
  process.exit(1);
}
console.log(`[loader] ${fibers.length} plugin(s) running`);

// 没有常驻句柄的插件组合（比如全替身）会让进程直接退出，这里保活
const keepAlive = setInterval(() => {}, 60_000);

let stopping = false;
const stop = () => {
  if (stopping) return;
  stopping = true;
  clearInterval(keepAlive);
  void (async () => {
    for (const { file, fiber } of [...fibers].reverse()) {
      try {
        await fiber.dispose();
      } catch (error) {
        console.error(`[loader] ${file} dispose 失败:`, error);
      }
    }
    // process.exit 绕过 B 站 WebSocket 约 5 秒的关闭握手
    process.exit(0);
  })();
};
process.on('SIGINT', stop);
process.on('SIGTERM', stop);
