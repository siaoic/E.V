import { execFile } from 'node:child_process';
import { mkdtemp, readFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { promisify } from 'node:util';
import { describe, expect, it } from 'vitest';
import { openNoteWorkspace } from './notes.ts';

const tmpDir = () => mkdtemp(path.join(tmpdir(), 'notes-'));

describe('NoteWorkspace', () => {
  // 每个 git 提交在 Windows 上约 0.4s，用例里多次提交，超时放宽到 7s 上限
  it('写/读/追加/编辑/删除全链路，list 返回相对路径', { timeout: 7000 }, async () => {
    const notes = await openNoteWorkspace(await tmpDir());

    await notes.write('notes/剪片心得.md', '# 心得\n转场要留白。');
    await notes.append('notes/剪片心得.md', '别用默认字幕。');
    expect(await notes.read('notes/剪片心得.md')).toContain('转场要留白');
    expect(await notes.read('notes/剪片心得.md')).toContain('别用默认字幕');

    expect(await notes.edit('notes/剪片心得.md', '留白', '呼吸感')).toBe(true);
    expect(await notes.read('notes/剪片心得.md')).toContain('呼吸感');
    expect(await notes.edit('notes/剪片心得.md', '不存在的原文', 'x')).toBe(false);

    await notes.write('viewers/1001.md', '常客，爱发猫图。');
    expect(await notes.list('notes')).toEqual(['notes/剪片心得.md']);
    expect(await notes.list('viewers')).toEqual(['viewers/1001.md']);

    expect(await notes.remove('viewers/1001.md')).toBe(true);
    expect(await notes.remove('viewers/1001.md')).toBe(false);
    expect(await notes.list('viewers')).toEqual([]);
  });

  it('每次写操作都有 git 提交，git_log/git_show 可回溯', { timeout: 7000 }, async () => {
    const dir = await tmpDir();
    const notes = await openNoteWorkspace(dir);

    await notes.write('notes/a.md', '第一版');
    await notes.write('notes/a.md', '第二版');

    const log = await notes.gitLog('notes/a.md');
    expect(log).toMatch(/write: notes\/a\.md/);

    const hash = log.split('\n')[0]!.split(' ')[0]!;
    expect(await notes.gitShow(hash)).toContain('第二版');
    expect(await notes.gitShow('deadbeef')).toContain('看不到');
    expect(await notes.gitLog(undefined, 5).then((s) => s.split('\n').length)).toBeGreaterThan(0);
  });

  it('覆写缩水过半时返回 shrinkFrom', { timeout: 7000 }, async () => {
    const notes = await openNoteWorkspace(await tmpDir());
    const long = Array.from({ length: 40 }, (_, i) => `第${i}行的内容`).join('\n');

    await notes.write('notes/long.md', long);
    expect((await notes.write('notes/long.md', '短了')).shrinkFrom).toBe(long.length);
    expect((await notes.write('notes/long.md', '短的但比上一版长一点，不触发')).shrinkFrom).toBeUndefined();
  });

  it('路径越出工作区被拒绝', async () => {
    const notes = await openNoteWorkspace(await tmpDir());

    await expect(notes.write('../evil.md', 'x')).rejects.toThrow('越出工作区');
    await expect(notes.read('notes/../../../evil.md')).rejects.toThrow('越出工作区');
  });

  it('.gitignore 忽略 memory.jsonl，git init 幂等', { timeout: 7000 }, async () => {
    const dir = await tmpDir();
    await openNoteWorkspace(dir);
    await openNoteWorkspace(dir);
    const notes = await openNoteWorkspace(dir);
    await notes.write('notes/a.md', '内容');

    expect(await readFile(path.join(dir, '.gitignore'), 'utf8')).toContain('memory.jsonl');
    const git = promisify(execFile);
    const { stdout } = await git('git', ['log', '--oneline'], { cwd: dir });
    expect(stdout).toContain('write: notes/a.md');
  });
});
