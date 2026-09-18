/**
 * 扩展是 extensions/ 下的 npm 包，manifest.kind 为 world、provider 或 bot。
 * 启动时读取直接依赖并分别校验 manifest、默认导出、id 命名空间与控制台页前缀。
 * 单包失败不阻止其他包加载；安装或卸载后需重启进程，ESM 模块不会在运行时替换。
 * 仅导入 deployment.json 选定的 bot；其他 bot 包记为 idle，仓内同名 bot 优先。
 * 扩展包目录只读，避免修改 pnpm store 的硬链接文件；模板写入规则见 src/bot.ts。
 * 扩展提供包内相对产物路径，服务端校验后分配 URL，只提供已声明文件。
 */
import { spawn } from 'node:child_process';
import { existsSync, mkdirSync, readFileSync, statSync, writeFileSync } from 'node:fs';
import { isAbsolute, join, resolve, sep } from 'node:path';
import { pathToFileURL } from 'node:url';
import type { CoreConfig, Logger } from './core/types.ts';
import type { BotDefinition } from './bot.ts';
import type { WorldDefinition, WorldSection } from './world.ts';
import type { ProviderModule } from './providers/base.ts';
import type { ExtensionInfo, ExtensionInstallTarget, ExtensionSearchHit } from './web/server.ts';
import {
  EXTENSION_KEYWORDS,
  parseExtensionManifest,
  type ExtensionConsoleAsset,
  type ExtensionKind,
  type ExtensionPackageJson,
} from './extensions/manifest.ts';
import { registerFrameworkResolver } from './extensions/runtime.ts';
import { pageIdFor, type ContributingKind } from './web/shared/console-protocol.ts';

export const EXTENSIONS_DIRNAME = 'extensions';

/** 每类扩展的控制台页前缀。 */
export const EXTENSION_PAGE_KIND: Readonly<Record<ExtensionKind, ContributingKind>> = {
  world: 'world',
  provider: 'llm',
  bot: 'persona',
};
const NPM_REGISTRY = 'https://registry.npmjs.org';

/** 一个已安装包在本进程启动时的加载结果。 */
export interface ExtensionRecord {
  name: string;
  /** extensions/package.json 里写的版本范围或 `link:` 路径。 */
  spec: string;
  /** 磁盘上的版本;node_modules 里没有这个包时为 null。 */
  version: string | null;
  description?: string;
  /** manifest 里的类别;package.json 解析不出 manifest 时缺席。 */
  kind?: ExtensionKind;
  /** 扩展声明的契约版本(`cortico.api`)。 */
  api?: number;
  /** 包声明了浏览器端产物(`cortico.consoleClient`)。 */
  consoleClient: boolean;
  /** 浏览器产物状态：none=未声明，served=文件可提供，missing=声明的文件不存在。 */
  console?: 'none' | 'served' | 'missing';
  loaded: boolean;
  /** bot 包装了但这份部署没引用它:没有 import,不算失败。 */
  idle?: true;
  reason?: string;
  /** 默认导出的 id:World id、provider id 或 bot id,按 kind 解释。 */
  worldId?: string;
  label?: string;
}

export interface ExtensionSet {
  dir: string;
  records: ExtensionRecord[];
  worlds: WorldDefinition<WorldSection>[];
  providers: ProviderModule[];
  /** 已加载的扩展里带浏览器端产物的那些;服务端据此分配 URL 并只发这几个文件。 */
  consoleAssets: ExtensionConsoleAsset[];
  /** 这份部署的 bot 来自扩展包时是它;仓内 bot 则缺席。 */
  bot?: ActiveBotPackage;
}

/** 启动器已经 import 过的那个 bot 包:包名与 `BotDefinition.id`。 */
export interface ActiveBotPackage {
  name: string;
  id: string;
}

export function extensionsDir(repoRoot: string): string {
  return join(repoRoot, EXTENSIONS_DIRNAME);
}

function readPackageJson(file: string): ExtensionPackageJson | null {
  if (!existsSync(file)) return null;
  return JSON.parse(readFileSync(file, 'utf8')) as ExtensionPackageJson;
}

/** 读取 extensions/package.json 的直接依赖；目录不存在时返回空列表。 */
export function readInstalled(dir: string): Array<{ name: string; spec: string }> {
  const pkg = readPackageJson(join(dir, 'package.json'));
  return Object.entries(pkg?.dependencies ?? {}).map(([name, spec]) => ({ name, spec }));
}

/**
 * 包的 ESM 入口:`exports`(字符串或 `.` 条目的 import/default)→ `module` → `main` → index.js。
 * 只认这几层;更复杂的 exports 条件表由包在 `main` 里给一个平坦入口。
 */
export function resolveExtensionEntry(pkgDir: string, pkg: ExtensionPackageJson): string {
  const pick = (v: unknown): string | null => {
    if (typeof v === 'string') return v;
    if (v && typeof v === 'object') {
      const o = v as Record<string, unknown>;
      return pick(o['.'] ?? o.import ?? o.default ?? null);
    }
    return null;
  };
  const rel = pick(pkg.exports) ?? pkg.module ?? pkg.main ?? 'index.js';
  return join(pkgDir, rel);
}

export function isWorldDefinition(v: unknown): v is WorldDefinition<WorldSection> {
  if (!v || typeof v !== 'object') return false;
  const d = v as Record<string, unknown>;
  return typeof d.id === 'string' && d.id !== ''
    && typeof d.label === 'string'
    && typeof d.defaults === 'function'
    && typeof d.create === 'function';
}

export function isProviderModule(v: unknown): v is ProviderModule {
  if (!v || typeof v !== 'object') return false;
  const d = v as Record<string, unknown>;
  return typeof d.id === 'string' && d.id !== ''
    && typeof d.title === 'string'
    && Array.isArray(d.reasoningTiers)
    && Array.isArray(d.serviceTiers)
    && typeof d.create === 'function';
}

export function isBotDefinition(v: unknown): v is BotDefinition<CoreConfig> {
  if (!v || typeof v !== 'object') return false;
  const d = v as Record<string, unknown>;
  return typeof d.id === 'string' && d.id !== ''
    && typeof d.defaults === 'function'
    && typeof d.build === 'function';
}

const SHAPE_NEEDS: Readonly<Record<ExtensionKind, string>> = {
  world: 'WorldDefinition 的 id / label / defaults() / create()',
  provider: 'ProviderModule 的 id / title / reasoningTiers / serviceTiers / create()',
  bot: 'BotDefinition 的 id / defaults() / build()',
};

/** 默认导出与 `cortico.kind` 对不上时给作者的一句话。装载器与 `check:extension` 共用。 */
export function extensionShapeMismatch(kind: ExtensionKind): string {
  return `声明是 ${kind} 类,但默认导出缺 ${SHAPE_NEEDS[kind]}。`;
}

/** 解析包内相对路径；超出包目录或文件不存在时返回 null。 */
export function extensionPackageFile(pkgDir: string, relative: string): string | null {
  const abs = resolve(pkgDir, relative);
  if (!abs.startsWith(resolve(pkgDir) + sep)) return null;
  return existsSync(abs) ? abs : null;
}

// bot 包:定位与 import(启动器在装载其它扩展之前做)

export type BotPackageLocation =
  | { source: 'tree'; pkgDir: string; entry: string }
  | { source: 'extension'; name: string; pkgDir: string; entry: string };

/**
 * `deployment.json` 的 `bot` 字段是包标识,两处找,仓内赢:`treeDir`(`bots/<bot>/`)下有
 * `index.ts` 就是仓内包;否则 `extensions/package.json` 的 dependencies 里有这个名字,且它的
 * manifest 是 `kind: "bot"`。都没有就报错,两处路径都列出来。
 */
export function locateBotPackage(repoRoot: string, ref: string, treeDir: string): BotPackageLocation {
  const treeEntry = join(treeDir, 'index.ts');
  if (existsSync(treeEntry)) return { source: 'tree', pkgDir: treeDir, entry: treeEntry };
  const dir = extensionsDir(repoRoot);
  if (!readInstalled(dir).some((p) => p.name === ref)) {
    throw new Error(`找不到 bot 代码包「${ref}」:仓内没有 ${treeEntry},${dir} 下也没装叫这个名字的扩展。`);
  }
  const pkgDir = join(dir, 'node_modules', ...ref.split('/'));
  const pkg = readPackageJson(join(pkgDir, 'package.json'));
  if (!pkg) throw new Error(`扩展「${ref}」登记在 ${dir} 的 package.json 里,但 node_modules 里没有它;在 extensions/ 下重新安装一次。`);
  const parsed = parseExtensionManifest(pkg);
  if (!parsed.ok) throw new Error(`扩展「${ref}」装不上: ${parsed.reasons.join(';')}`);
  if (parsed.manifest.kind !== 'bot') {
    throw new Error(`「${ref}」是 ${parsed.manifest.kind} 类扩展,不是 bot 包;deployment.json 的 bot 字段要指向 cortico.kind 为 "bot" 的包。`);
  }
  return { source: 'extension', name: ref, pkgDir, entry: resolveExtensionEntry(pkgDir, pkg) };
}

/**
 * import 定位到的 bot 包并校验默认导出。扩展来源的 bot 其 id 不得与仓内 `bots/` 任一目录同名:
 * 控制台按 `persona:<id>` 找面板产物,构建产物表里同 key 的仓内页优先,撞名会拿到仓内那份面板。
 */
export async function importBotDefinition(
  location: BotPackageLocation,
  opts: { treeHas: (id: string) => boolean },
): Promise<BotDefinition<CoreConfig>> {
  registerFrameworkResolver();
  const mod = (await import(pathToFileURL(location.entry).href)) as { default?: unknown };
  if (!isBotDefinition(mod.default)) {
    throw new Error(location.source === 'tree'
      ? `${location.entry} 没有默认导出一个 BotDefinition`
      : `扩展「${location.name}」${extensionShapeMismatch('bot')}`);
  }
  if (location.source === 'extension' && opts.treeHas(mod.default.id)) {
    throw new Error(`扩展「${location.name}」的 bot id「${mod.default.id}」与仓内 bots/${mod.default.id}/ 撞名;控制台面板产物按 persona:<id> 找,会拿到仓内那份。换一个 id。`);
  }
  return mod.default;
}

/**
 * 加载扩展 World 与 provider；id 与内建冲突时拒绝，扩展间重名时保留先加载项。
 * 两个 kind 使用独立命名空间。activeBot 已由启动器导入，此处只标记 loaded，其他 bot 记 idle。
 */
export async function loadExtensions(
  repoRoot: string,
  opts: { reserved?: Iterable<string>; reservedProviders?: Iterable<string>; activeBot?: ActiveBotPackage; log?: Logger } = {},
): Promise<ExtensionSet> {
  registerFrameworkResolver();
  const dir = extensionsDir(repoRoot);
  const taken: Record<Exclude<ExtensionKind, 'bot'>, Set<string>> = {
    world: new Set(opts.reserved ?? []),
    provider: new Set(opts.reservedProviders ?? []),
  };
  const records: ExtensionRecord[] = [];
  const worlds: WorldDefinition<WorldSection>[] = [];
  const providers: ProviderModule[] = [];
  const consoleAssets: ExtensionConsoleAsset[] = [];
  for (const { name, spec } of readInstalled(dir)) {
    const pkgDir = join(dir, 'node_modules', ...name.split('/'));
    const pkg = readPackageJson(join(pkgDir, 'package.json'));
    const record: ExtensionRecord = {
      name,
      spec,
      version: pkg?.version ?? null,
      consoleClient: false,
      loaded: false,
      ...(pkg?.description ? { description: pkg.description } : {}),
    };
    records.push(record);
    if (!pkg) {
      record.reason = 'node_modules 里没有这个包;在 extensions/ 下重新安装一次。';
      continue;
    }
    const parsed = parseExtensionManifest(pkg);
    for (const warning of parsed.warnings) opts.log?.warn(`扩展 manifest: ${name} — ${warning}`);
    if (!parsed.ok) {
      record.reason = parsed.reasons.join(';');
      continue;
    }
    const { kind, api, consoleClient, consoleStyle } = parsed.manifest;
    record.kind = kind;
    record.api = api;
    record.consoleClient = consoleClient !== undefined;

    // 缺少面板产物不阻止定义加载。
    const jsFile = consoleClient === undefined ? null : extensionPackageFile(pkgDir, consoleClient);
    record.console = consoleClient === undefined ? 'none' : jsFile ? 'served' : 'missing';
    if (consoleClient !== undefined && !jsFile) {
      opts.log?.warn(`扩展声明的控制台产物不在: ${name} — ${consoleClient}`);
    }
    const cssFile = consoleStyle !== undefined && jsFile ? extensionPackageFile(pkgDir, consoleStyle) : null;
    if (consoleStyle !== undefined && jsFile && !cssFile) {
      opts.log?.warn(`扩展声明的控制台样式不在,面板按无样式发: ${name} — ${consoleStyle}`);
    }

    const serveAsset = (id: string): void => {
      if (!jsFile) return;
      consoleAssets.push({
        pageId: pageIdFor(EXTENSION_PAGE_KIND[kind], id),
        packageName: name,
        version: pkg.version ?? '0',
        jsFile,
        ...(cssFile ? { cssFile } : {}),
      });
    };

    if (kind === 'bot') {
      if (opts.activeBot?.name !== name) {
        record.idle = true;
        continue;
      }
      record.worldId = opts.activeBot.id;
      record.loaded = true;
      serveAsset(opts.activeBot.id);
      continue;
    }

    const entry = resolveExtensionEntry(pkgDir, pkg);
    let exported: unknown;
    try {
      exported = ((await import(pathToFileURL(entry).href)) as { default?: unknown }).default;
    } catch (error) {
      record.reason = `导入失败: ${error instanceof Error ? error.message : String(error)}`;
      opts.log?.warn(`扩展导入失败: ${name}`, { entry, error: record.reason });
      continue;
    }

    let id: string;
    if (kind === 'world') {
      if (!isWorldDefinition(exported)) {
        record.reason = extensionShapeMismatch('world');
        continue;
      }
      record.worldId = exported.id;
      if (taken.world.has(exported.id)) {
        record.reason = `World id「${exported.id}」已被内建 World 或先加载的扩展占用。`;
        continue;
      }
      taken.world.add(exported.id);
      record.label = exported.label;
      worlds.push(exported);
      id = exported.id;
    } else {
      if (!isProviderModule(exported)) {
        record.reason = extensionShapeMismatch('provider');
        continue;
      }
      record.worldId = exported.id;
      if (taken.provider.has(exported.id)) {
        record.reason = `provider id「${exported.id}」已被内建 provider 或先加载的扩展占用。`;
        continue;
      }
      taken.provider.add(exported.id);
      record.label = exported.title;
      providers.push(exported);
      id = exported.id;
    }
    record.loaded = true;
    serveAsset(id);
  }
  const bot = opts.activeBot && records.some((r) => r.loaded && r.kind === 'bot') ? opts.activeBot : undefined;
  return { dir, records, worlds, providers, consoleAssets, ...(bot ? { bot } : {}) };
}

// 装卸与搜索(控制台那一面)

/** npm 包名。与 npm 自己的校验同形;不含任何 shell 元字符。 */
const PACKAGE_NAME = /^(@[a-z0-9-~][a-z0-9-._~]*\/)?[a-z0-9-~][a-z0-9-._~]*$/;
/**
 * 版本或 dist-tag。`^` `~` `*` 之外的范围符号(`>=` `||`)不收:它们在 cmd.exe 里是
 * 重定向与管道,而 Windows 上 corepack 只能经 shell 起。
 */
const VERSION_SPEC = /^[0-9a-zA-Z.^~*+-]{1,64}$/;
/** 本地目录。排除 `%` `!` `"` 与重定向符,其余交给引号。 */
const LOCAL_PATH = /^[A-Za-z0-9_.\-/:\\ ~]{1,512}$/;

export type PackageManagerRunner = (args: string[], cwd: string) => Promise<{ code: number; output: string }>;

/** Windows 通过 shell 调用 corepack 的 .cmd 文件，参数逐个加引号。 */
export const runPnpm: PackageManagerRunner = (args, cwd) => new Promise((done, fail) => {
  const viaShell = process.platform === 'win32';
  const argv = ['pnpm', ...args].map((a) => (viaShell ? `"${a}"` : a));
  const child = spawn('corepack', argv, {
    cwd,
    shell: viaShell,
    windowsHide: true,
    env: { ...process.env, COREPACK_ENABLE_DOWNLOAD_PROMPT: '0' },
  });
  let output = '';
  child.stdout.on('data', (chunk: Buffer) => { output += chunk.toString(); });
  child.stderr.on('data', (chunk: Buffer) => { output += chunk.toString(); });
  child.on('error', fail);
  child.on('close', (code) => done({ code: code ?? -1, output }));
});

interface RegistrySearchResponse {
  objects?: Array<{
    package?: {
      name?: string;
      version?: string;
      description?: string;
      keywords?: string[];
      date?: string;
      links?: { npm?: string; homepage?: string; repository?: string };
      publisher?: { username?: string };
    };
    downloads?: { monthly?: number };
  }>;
}

export interface ExtensionManagerOptions {
  run?: PackageManagerRunner;
  registry?: string;
  fetchJson?: (url: string) => Promise<unknown>;
}

/** 扩展管理接口；安装与卸载串行执行，避免并发修改同一依赖目录。 */
export class ExtensionManager {
  private readonly dir: string;
  private readonly run: PackageManagerRunner;
  private readonly registry: string;
  private readonly fetchJson: (url: string) => Promise<unknown>;
  private busy = false;

  constructor(
    private readonly repoRoot: string,
    private readonly booted: ExtensionSet,
    opts: ExtensionManagerOptions = {},
  ) {
    this.dir = booted.dir;
    this.run = opts.run ?? runPnpm;
    this.registry = (opts.registry ?? NPM_REGISTRY).replace(/\/$/, '');
    this.fetchJson = opts.fetchJson ?? (async (url) => {
      const res = await fetch(url);
      if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
      return res.json();
    });
  }

  /** 已加载扩展的浏览器端产物。服务端据此把页 id 映到 URL 并只发这几个文件。 */
  consoleAssets(): readonly ExtensionConsoleAsset[] {
    return this.booted.consoleAssets;
  }

  /** 将启动时的加载结果与当前安装状态比较；不一致时标记待重启。 */
  list(): { dir: string; extensions: ExtensionInfo[] } {
    const onDisk = new Map(readInstalled(this.dir).map((p) => [p.name, p.spec]));
    const out: ExtensionInfo[] = [];
    for (const r of this.booted.records) {
      const spec = onDisk.get(r.name);
      onDisk.delete(r.name);
      const state: ExtensionInfo['state'] = spec === undefined ? 'removed'
        : spec !== r.spec ? 'pending-restart'
        : r.loaded ? 'loaded' : r.idle ? 'idle' : 'failed';
      out.push({ ...r, state });
    }
    // 新装包仅报告 manifest 声明，浏览器产物状态在下一次加载时确定。
    for (const [name, spec] of onDisk) {
      const pkg = readPackageJson(join(this.dir, 'node_modules', ...name.split('/'), 'package.json'));
      const parsed = pkg ? parseExtensionManifest(pkg) : null;
      const manifest = parsed?.ok ? parsed.manifest : null;
      out.push({
        name,
        spec,
        version: pkg?.version ?? null,
        consoleClient: manifest?.consoleClient !== undefined,
        ...(manifest ? { kind: manifest.kind, api: manifest.api } : {}),
        ...(manifest && manifest.consoleClient === undefined ? { console: 'none' as const } : {}),
        loaded: false,
        state: 'pending-restart',
        ...(pkg?.description ? { description: pkg.description } : {}),
      });
    }
    return { dir: this.dir, extensions: out };
  }

  async search(query: string, kind: ExtensionKind = 'world'): Promise<ExtensionSearchHit[]> {
    const keyword = EXTENSION_KEYWORDS[kind];
    const text = `keywords:${keyword} ${query.trim()}`.trim();
    const url = `${this.registry}/-/v1/search?text=${encodeURIComponent(text)}&size=50`;
    const data = (await this.fetchJson(url)) as RegistrySearchResponse;
    const installed = new Set(readInstalled(this.dir).map((p) => p.name));
    const hits: ExtensionSearchHit[] = [];
    for (const obj of data.objects ?? []) {
      const p = obj.package;
      if (!p?.name || !p.version || !(p.keywords ?? []).includes(keyword)) continue;
      hits.push({
        name: p.name,
        version: p.version,
        description: p.description ?? '',
        kind,
        ...(p.date ? { date: p.date } : {}),
        ...(p.publisher?.username ? { publisher: p.publisher.username } : {}),
        downloads: obj.downloads?.monthly ?? 0,
        links: {
          ...(p.links?.npm ? { npm: p.links.npm } : {}),
          ...(p.links?.repository ? { repository: p.links.repository } : {}),
          ...(p.links?.homepage ? { homepage: p.links.homepage } : {}),
        },
        installed: installed.has(p.name),
      });
    }
    return hits;
  }

  async install(target: ExtensionInstallTarget): Promise<string> {
    const spec = this.installSpec(target);
    if (!existsSync(this.dir)) mkdirSync(this.dir, { recursive: true });
    const pkgFile = join(this.dir, 'package.json');
    if (!existsSync(pkgFile)) {
      writeFileSync(pkgFile, JSON.stringify({ name: 'cortico-extensions', private: true, dependencies: {} }, null, 2) + '\n', 'utf8');
    }
    const output = await this.exclusive(['add', spec, '--ignore-workspace']);
    return `已安装 ${spec}。重启进程后加载。\n${output}`;
  }

  async uninstall(name: string): Promise<string> {
    if (!PACKAGE_NAME.test(name)) throw new Error(`不是合法的包名: ${name}`);
    if (!readInstalled(this.dir).some((p) => p.name === name)) throw new Error(`没有安装这个包: ${name}`);
    const output = await this.exclusive(['remove', name, '--ignore-workspace']);
    return `已卸载 ${name}。重启进程后生效。\n${output}`;
  }

  private installSpec(target: ExtensionInstallTarget): string {
    if ('path' in target) {
      const raw = target.path.trim();
      if (!LOCAL_PATH.test(raw)) throw new Error('路径含有不接受的字符。');
      const abs = isAbsolute(raw) ? raw : resolve(this.repoRoot, raw);
      if (!existsSync(abs) || !statSync(abs).isDirectory()) throw new Error(`目录不存在: ${abs}`);
      if (!existsSync(join(abs, 'package.json'))) throw new Error(`目录里没有 package.json: ${abs}`);
      return abs;
    }
    const name = target.name.trim();
    if (!PACKAGE_NAME.test(name)) throw new Error(`不是合法的 npm 包名: ${name}`);
    const version = target.version?.trim() ?? '';
    if (version && !VERSION_SPEC.test(version)) throw new Error(`不是合法的版本: ${version}`);
    return version ? `${name}@${version}` : name;
  }

  private async exclusive(args: string[]): Promise<string> {
    if (this.busy) throw new Error('已有一个安装 / 卸载在进行,等它结束。');
    this.busy = true;
    try {
      const { code, output } = await this.run(args, this.dir);
      const tail = output.trim().split('\n').slice(-20).join('\n');
      if (code !== 0) throw new Error(`pnpm ${args[0]} 退出码 ${code}:\n${tail}`);
      return tail;
    } finally {
      this.busy = false;
    }
  }
}
