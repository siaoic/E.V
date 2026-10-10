import { execFile } from 'node:child_process';
import { mkdir, readdir, readFile, unlink, writeFile, appendFile } from 'node:fs/promises';
import path from 'node:path';
import { promisify } from 'node:util';

const runGit = promisify(execFile);

// git 身份只作用于本工作区仓库（-c 每次注入，不碰全局配置）
const GIT_IDENTITY = ['-c', 'user.name=fuyu', '-c', 'user.email=fuyu@workspace'];

export type NoteWorkspace = {
  readonly root: string;
  read(rel: string): Promise<string>;
  /** 覆写；新内容不足原来一半时返回 shrinkFrom（原字节数）供回执点名 */
  write(rel: string, content: string): Promise<{ shrinkFrom?: number }>;
  append(rel: string, text: string): Promise<void>;
  /** 精确匹配替换第一处；未命中返回 false */
  edit(rel: string, match: string, replace: string): Promise<boolean>;
  remove(rel: string): Promise<boolean>;
  /** 列出 prefix（如 notes/、viewers/）下的相对路径 */
  list(prefix: string): Promise<string[]>;
  /** 指定文件入库提交；git 缺席或失败时静默降级为纯文件操作 */
  commitFiles(rels: string[], message: string): Promise<void>;
  gitLog(rel: string | undefined, n?: number): Promise<string>;
  gitShow(hash: string): Promise<string>;
};

export async function openNoteWorkspace(root: string): Promise<NoteWorkspace> {
  root = path.resolve(root);
  await mkdir(root, { recursive: true });
  try {
    await runGit('git', ['init'], { cwd: root });
  } catch {
    // git 缺席：整个版本层降级，文件操作照常
  }
  try {
    const ignore = path.join(root, '.gitignore');
    const existing = await readFile(ignore, 'utf8').catch(() => '');
    if (!existing.split('\n').map((l) => l.trim()).includes('memory.jsonl')) {
      await writeFile(ignore, existing ? `${existing.trimEnd()}\nmemory.jsonl\n` : 'memory.jsonl\n', 'utf8');
    }
  } catch {
    // 忽略文件写不进也不阻塞
  }

  // 所有操作串行，避免 git index.lock 争用
  let queue: Promise<unknown> = Promise.resolve();
  const enqueue = <T>(task: () => Promise<T>): Promise<T> => {
    const next = queue.then(task, task);
    queue = next.catch(() => {});
    return next;
  };

  const git = (args: string[]): Promise<string> =>
    runGit('git', [...GIT_IDENTITY, ...args], { cwd: root }).then(
      ({ stdout }) => String(stdout),
      () => '',
    );

  const resolve = (rel: string): string => {
    const abs = path.resolve(root, rel.replace(/^[/\\]+/, ''));
    if (abs !== root && !abs.startsWith(root + path.sep)) throw new Error('路径不能越出工作区');
    return abs;
  };

  const toRel = (rel: string): string => path.posix.normalize(rel.replace(/\\/g, '/')).replace(/^\/+/, '');

  const rawCommit = async (rels: string[], message: string): Promise<void> => {
    const files = rels.map(toRel);
    await git(['add', ...files]);
    await git(['commit', '-m', message]);
  };
  const commitFiles = (rels: string[], message: string): Promise<void> => enqueue(() => rawCommit(rels, message));

  return {
    root,
    async read(rel: string): Promise<string> {
      const file = resolve(rel);
      return enqueue(() => readFile(file, 'utf8'));
    },
    async write(rel: string, content: string): Promise<{ shrinkFrom?: number }> {
      const file = resolve(rel);
      const r = toRel(rel);
      return enqueue(async () => {
        const old = await readFile(file, 'utf8').catch(() => '');
        await mkdir(path.dirname(file), { recursive: true });
        await writeFile(file, content, 'utf8');
        await rawCommit([r], `write: ${r}`);
        return old.trim() && content.length < old.length / 2 ? { shrinkFrom: old.length } : {};
      });
    },
    async append(rel: string, text: string): Promise<void> {
      const file = resolve(rel);
      const r = toRel(rel);
      await enqueue(async () => {
        await mkdir(path.dirname(file), { recursive: true });
        await appendFile(file, text.endsWith('\n') ? text : `${text}\n`, 'utf8');
        await rawCommit([r], `append: ${r}`);
      });
    },
    async edit(rel: string, match: string, replace: string): Promise<boolean> {
      const file = resolve(rel);
      const r = toRel(rel);
      return enqueue(async () => {
        const old = await readFile(file, 'utf8').catch(() => '');
        const at = old.indexOf(match);
        if (at === -1) return false;
        const next = old.slice(0, at) + replace + old.slice(at + match.length);
        await writeFile(file, next, 'utf8');
        await rawCommit([r], `edit: ${r}`);
        return true;
      });
    },
    async remove(rel: string): Promise<boolean> {
      const file = resolve(rel);
      const r = toRel(rel);
      return enqueue(async () => {
        try {
          await unlink(file);
        } catch {
          return false;
        }
        await rawCommit([r], `delete: ${r}`);
        return true;
      });
    },
    async list(prefix: string): Promise<string[]> {
      const base = resolve(prefix);
      const walk = async (dir: string): Promise<string[]> => {
        const entries = await readdir(dir, { withFileTypes: true }).catch(() => []);
        const files: string[] = [];
        for (const entry of entries) {
          const abs = path.join(dir, entry.name);
          if (entry.isDirectory()) files.push(...(await walk(abs)));
          else files.push(path.relative(root, abs).split(path.sep).join('/'));
        }
        return files;
      };
      return enqueue(() => walk(base));
    },
    commitFiles,
    async gitLog(rel: string | undefined, n = 5): Promise<string> {
      const args = ['log', '--oneline', `-n${n}`];
      if (rel) args.push('--', toRel(rel));
      return (await git(args)).trim();
    },
    async gitShow(hash: string): Promise<string> {
      return runGit('git', ['show', hash], { cwd: root })
        .then(({ stdout }) => String(stdout))
        .catch(() => '（看不到那个版本）');
    },
  };
}
