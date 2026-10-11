import { execFile } from 'node:child_process';
import { mkdir, readdir, readFile, rename, unlink, writeFile, appendFile } from 'node:fs/promises';
import path from 'node:path';
import { promisify } from 'node:util';
import { shrinkNote } from './shrink.ts';

const runGit = promisify(execFile);

// git 身份只作用于本工作区仓库（-c 每次注入，不碰全局配置）
const GIT_IDENTITY = ['-c', 'user.name=fuyu', '-c', 'user.email=fuyu@workspace'];

export type Checkpoint = { name: string; message: string; hash: string; date: string };

export type NoteWorkspace = {
  readonly root: string;
  read(rel: string): Promise<string>;
  /** 覆写；比上一版短很多时返回 shrinkNote（点名将要消失的小节）供回执追加 */
  write(rel: string, content: string): Promise<{ shrinkNote?: string }>;
  append(rel: string, text: string): Promise<void>;
  /** 精确匹配替换第一处；未命中返回 false */
  edit(rel: string, match: string, replace: string): Promise<boolean>;
  remove(rel: string): Promise<boolean>;
  /** 列出 prefix（如 notes/、viewers/）下的相对路径 */
  list(prefix: string): Promise<string[]>;
  /** 指定文件入库提交；git 缺席或失败时静默降级为纯文件操作 */
  commitFiles(rels: string[], message: string): Promise<void>;
  /** 建立命名检查点（annotated tag）：先提交当前改动，再打标签；重名或 git 缺席时抛错 */
  checkpoint(name: string, message?: string): Promise<void>;
  /** 列出检查点（新的在前）；checkpoint0 是出厂基线 */
  checkpoints(): Promise<Checkpoint[]>;
  /** 回滚工作区到某个检查点（reset --hard）；未跟踪文件不受影响 */
  rollback(name: string): Promise<void>;
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

  // 出厂基线：仓库还没有提交时先落一个 checkpoint0（annotated tag），
  // 与 Cortico 的 workspaceGit 同一做法（init 即建 checkpoint0，回滚用 reset --hard）。
  // 任何一次写入因此都有退路：人格档案被坏内容覆盖时能回滚，而不是无从追溯。
  const hasHead = await runGit('git', ['rev-parse', '--verify', 'HEAD'], { cwd: root }).then(
    () => true,
    () => false,
  );
  if (!hasHead) {
    try {
      await runGit('git', [...GIT_IDENTITY, 'add', '-A'], { cwd: root });
      await runGit('git', [...GIT_IDENTITY, 'commit', '--allow-empty', '-m', 'checkpoint0: 出厂基线'], { cwd: root });
      await runGit('git', [...GIT_IDENTITY, 'tag', '-a', 'checkpoint0', '-m', 'checkpoint0: 出厂基线'], { cwd: root });
    } catch {
      // git 缺席：整个版本层降级，文件操作照常
    }
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

  /** 与 git 相反：失败要抛错。回滚与检查点这类操作必须让调用方知道没成功。 */
  const gitStrict = (args: string[]): Promise<string> =>
    runGit('git', [...GIT_IDENTITY, ...args], { cwd: root }).then(({ stdout }) => String(stdout));

  // 检查点名先卡形状（对齐 Cortico workspaceGit 的 tag 名校验）：不能以 - 开头，只留安全字形
  const assertTagName = (name: string): string => {
    const trimmed = name.trim();
    if (!trimmed || trimmed.startsWith('-') || !/^[\w.\-\u4e00-\u9fa5]+$/.test(trimmed)) {
      throw new Error(`检查点名只能含字母数字、下划线、点、连字符或中文：${name}`);
    }
    return trimmed;
  };

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
    async write(rel: string, content: string): Promise<{ shrinkNote?: string }> {
      const file = resolve(rel);
      const r = toRel(rel);
      return enqueue(async () => {
        const old = await readFile(file, 'utf8').catch(() => '');
        await mkdir(path.dirname(file), { recursive: true });
        // 原子写：同目录临时文件 + rename（对齐 Cortico `memory.ts:290-309`），
        // 中途崩掉也不会留下半个文件；失败时保留原始错误并清掉临时文件。
        const tmp = `${file}.tmp-${process.pid}-${Date.now()}`;
        try {
          await writeFile(tmp, content, 'utf8');
          await rename(tmp, file);
        } catch (error) {
          await unlink(tmp).catch(() => {});
          throw error;
        }
        await rawCommit([r], `write: ${r}`);
        const note = old ? shrinkNote(old, content) : null;
        return note ? { shrinkNote: note } : {};
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
    checkpoint(name: string, message?: string): Promise<void> {
      return enqueue(async () => {
        const tag = assertTagName(name);
        const note = message ?? `checkpoint「${tag}」`;
        await git(['add', '-A']);
        await git(['commit', '--allow-empty', '-m', note]);
        await gitStrict(['tag', '-a', tag, '-m', note]);
      });
    },
    checkpoints(): Promise<Checkpoint[]> {
      return enqueue(async () => {
        const out = await git([
          'for-each-ref',
          '--format=%(refname:short)\t%(objectname:short)\t%(subject)\t%(creatordate:short)',
          'refs/tags',
        ]);
        return out
          .split('\n')
          .map((line) => line.trim())
          .filter(Boolean)
          .map((line) => {
            const [name = '', hash = '', message = '', date = ''] = line.split('\t');
            return { name, hash, message, date };
          })
          .reverse();
      });
    },
    rollback(name: string): Promise<void> {
      return enqueue(async () => {
        const tag = assertTagName(name);
        await gitStrict(['rev-parse', '--verify', `refs/tags/${tag}`]);
        await gitStrict(['reset', '--hard', tag]);
      });
    },
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
