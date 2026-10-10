import { mkdtemp, readFile, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { describe, expect, it } from 'vitest';
import { openPersonaStore } from './persona.ts';

const tmpDir = () => mkdtemp(path.join(tmpdir(), 'persona-'));

describe('PersonaStore', () => {
  it('首次打开引导创建宪法种子，prompt 渲染种子', async () => {
    const dir = await tmpDir();
    const persona = await openPersonaStore(dir);
    const seed = await readFile(path.join(dir, 'CONSTITUTION.seed.md'), 'utf8');

    expect(seed).toContain('黛冬优子');
    expect(persona.prompt()).toBe(seed.trim());
  });

  it('已有种子不被覆写，prompt 以文件为准', async () => {
    const dir = await tmpDir();
    await writeFile(path.join(dir, 'CONSTITUTION.seed.md'), '# 我的种子\n安静。', 'utf8');

    const persona = await openPersonaStore(dir);
    const seed = await readFile(path.join(dir, 'CONSTITUTION.seed.md'), 'utf8');

    expect(seed).toBe('# 我的种子\n安静。');
    expect(persona.prompt()).toBe('# 我的种子\n安静。');
  });

  it('已有 PERSONA.md 进入 prompt 的自我认知小节', async () => {
    const dir = await tmpDir();
    await writeFile(path.join(dir, 'PERSONA.md'), '- [2026-10-10] 讨厌被催更', 'utf8');

    const persona = await openPersonaStore(dir);

    expect(persona.prompt()).toContain('# 自我认知');
    expect(persona.prompt()).toContain('讨厌被催更');
  });

  it('append 追加带日期的条目并即时反映到 prompt，空白不落盘', async () => {
    const dir = await tmpDir();
    const persona = await openPersonaStore(dir);

    await persona.append('我发现自己讲解失误时喜欢先嘴硬');
    await persona.append('   ');
    const file = await readFile(path.join(dir, 'PERSONA.md'), 'utf8');

    expect(file).toMatch(/^- \[\d{4}-\d{2}-\d{2}\] 我发现自己讲解失误时喜欢先嘴硬$/m);
    expect(file.split('\n').filter(Boolean).length).toBe(1);
    expect(persona.prompt()).toContain('# 自我认知');
    expect(persona.prompt()).toContain('先嘴硬');
  });

  it('revise 精确替换一行，留空即删除，未命中返回 false', async () => {
    const dir = await tmpDir();
    const persona = await openPersonaStore(dir);
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
    expect((await fileText())).not.toContain('第二条旧认知');
    expect(persona.prompt()).not.toContain('第二条旧认知');
  });
});
