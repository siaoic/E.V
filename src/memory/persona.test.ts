import { mkdtemp, readFile, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { describe, expect, it } from 'vitest';
import { openNoteWorkspace } from './notes.ts';
import { openPersonaStore } from './persona.ts';

// 人格档案不再自己写文件：全部落进带 Git 版本的笔记工作区
const setup = async () => {
  const dir = await mkdtemp(path.join(tmpdir(), 'persona-'));
  const notes = await openNoteWorkspace(dir);
  return { dir, notes, persona: await openPersonaStore(notes) };
};

// 预先放好文件再开工作区（种子/PERSONA.md 已存在的情形）
const withFile = async (name: string, content: string) => {
  const dir = await mkdtemp(path.join(tmpdir(), 'persona-'));
  await writeFile(path.join(dir, name), content, 'utf8');
  const notes = await openNoteWorkspace(dir);
  return { dir, notes, persona: await openPersonaStore(notes) };
};

describe('PersonaStore', () => {
  it('首次打开引导创建宪法种子，prompt 渲染种子', async () => {
    const { dir, persona } = await setup();
    const seed = await readFile(path.join(dir, 'CONSTITUTION.seed.md'), 'utf8');

    expect(seed).toContain('黛冬优子');
    expect(persona.prompt()).toBe(seed.trim());
  });

  it('已有种子不被覆写，prompt 以文件为准', async () => {
    const { dir, persona } = await withFile('CONSTITUTION.seed.md', '# 我的种子\n安静。');
    const seed = await readFile(path.join(dir, 'CONSTITUTION.seed.md'), 'utf8');

    expect(seed).toBe('# 我的种子\n安静。');
    expect(persona.prompt()).toBe('# 我的种子\n安静。');
  });

  it('已有 PERSONA.md 进入 prompt 的自我认知小节', async () => {
    const { persona } = await withFile('PERSONA.md', '- [2026-10-10] 讨厌被催更');

    expect(persona.prompt()).toContain('# 自我认知');
    expect(persona.prompt()).toContain('讨厌被催更');
  });

  it('append 追加带日期的条目并即时反映到 prompt，空白不落盘', async () => {
    const { dir, persona } = await setup();

    await persona.append('我发现自己讲解失误时喜欢先嘴硬');
    await persona.append('   ');
    const file = await readFile(path.join(dir, 'PERSONA.md'), 'utf8');

    expect(file).toMatch(/^- \[\d{4}-\d{2}-\d{2}\] 我发现自己讲解失误时喜欢先嘴硬$/m);
    expect(file.split('\n').filter(Boolean).length).toBe(1);
    expect(persona.prompt()).toContain('# 自我认知');
    expect(persona.prompt()).toContain('先嘴硬');
  });

  it('revise 精确替换一行，留空即删除，未命中返回 false', async () => {
    const { dir, persona } = await setup();
    await persona.append('第一条旧认知');
    await persona.append('第二条旧认知');

    expect(await persona.revise('- [2099-01-01] 不存在的一行', '替换词')).toBe(false);

    const fileText = () => readFile(path.join(dir, 'PERSONA.md'), 'utf8');
    const firstLine = (await fileText()).split('\n').find((l) => l.includes('第一条'))!;
    expect(await persona.revise(firstLine, '- 改过的第一条')).toBe(true);
    expect(persona.prompt()).toContain('- 改过的第一条');
    expect(persona.prompt()).not.toContain('第一条旧认知');

    const secondLine = (await fileText()).split('\n').find((l) => l.includes('第二条'))!;
    expect(await persona.revise(secondLine, '')).toBe(true);
    expect(await fileText()).not.toContain('第二条旧认知');
    expect(persona.prompt()).not.toContain('第二条旧认知');
  });

  it('自我认知的每次变更都留下 Git 版本，旧版取回得来', { timeout: 20000 }, async () => {
    const { notes, persona } = await setup();

    await persona.append('第一次沉淀');
    await persona.replace(['第一次沉淀', '后来蒸馏出的第二条']);

    const log = await notes.gitLog('PERSONA.md', 10);
    expect(log).toContain('append: PERSONA.md');
    expect(log).toContain('write: PERSONA.md');

    const hash = log.split('\n').find((line) => line.includes('append: PERSONA.md'))!.split(' ')[0]!;
    expect(await notes.gitShow(hash)).toContain('第一次沉淀');
  });
});
