import { mkdtemp, readFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { describe, expect, it, vi } from 'vitest';
import { createDream } from './dream.ts';
import { openNoteWorkspace } from './notes.ts';
import { openPersonaStore } from './persona.ts';

const setup = async () => {
  const dir = await mkdtemp(path.join(tmpdir(), 'dream-'));
  const notes = await openNoteWorkspace(dir);
  return { dir, notes, persona: await openPersonaStore(notes) };
};

describe('dream', () => {
  it('心跳计数触发整理，SUMMARY 进摘要，条目整档重写 PERSONA.md', { timeout: 7000 }, async () => {
    const { dir, notes, persona } = await setup();
    await persona.append('喜欢深夜剪片');
    await persona.append('喜欢深夜剪片子');
    const complete = vi.fn(async () => ({
      content: '- 喜欢在深夜剪片子\n- 对镜头感挑剔\nSUMMARY: 合并了重复的剪片偏好',
      toolCalls: [],
    }));
    const dream = createDream({ llm: { complete }, notes, persona, every: 2 });

    dream.tick();
    expect(complete).not.toHaveBeenCalled();
    dream.tick();
    await vi.waitFor(() => expect(complete).toHaveBeenCalledTimes(1));

    // 整理写完 PERSONA.md 要走一次 Git 提交，摘要要等这一步结束才成型
    await vi.waitFor(() => expect(dream.summary()).toBe('合并了重复的剪片偏好'));
    expect(persona.prompt()).toContain('对镜头感挑剔');
    const body = await readFile(path.join(dir, 'PERSONA.md'), 'utf8');
    expect(body.split('\n').filter(Boolean)).toHaveLength(2);
    expect(body).toContain('喜欢在深夜剪片子');
  });

  it('单实例排队：运行中重复触发不并发，结束后补一轮', { timeout: 7000 }, async () => {
    const { notes, persona } = await setup();
    await persona.append('原有认知');
    let release!: () => void;
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    const complete = vi.fn(async () => {
      await gate;
      return { content: 'SUMMARY: 完成', toolCalls: [] };
    });
    const dream = createDream({ llm: { complete }, notes, persona, every: 1 });

    dream.tick();
    dream.tick();
    dream.tick();
    await vi.waitFor(() => expect(complete).toHaveBeenCalledTimes(1));

    release();
    await vi.waitFor(() => expect(complete).toHaveBeenCalledTimes(2));
    expect(dream.summary()).toBe('完成');
  });

  it('LLM 失败时放弃本轮，旧认知保留、摘要为空', { timeout: 7000 }, async () => {
    const { notes, persona } = await setup();
    await persona.append('原有认知');
    const complete = vi.fn(async () => {
      throw new Error('超时');
    });
    const dream = createDream({ llm: { complete }, notes, persona, every: 1 });

    dream.tick();
    await vi.waitFor(() => expect(complete).toHaveBeenCalledTimes(1));
    await vi.waitFor(() => expect(dream.summary()).toBe(''));

    expect(persona.prompt()).toContain('原有认知');
  });

  it('空工作区不调用 LLM', { timeout: 7000 }, async () => {
    const { notes, persona } = await setup();
    const complete = vi.fn();
    const dream = createDream({ llm: { complete } as never, notes, persona, every: 1 });

    await dream.flush();

    expect(complete).not.toHaveBeenCalled();
  });

  // 对齐 Cortico：破坏性覆盖不拦写入，靠「摘要点名 + 每次写进 Git 可取回旧版」兜底
  it('整理结果大幅缩水时照常写入，但摘要点名且旧版在 Git 里可取回', { timeout: 20000 }, async () => {
    const { dir, notes, persona } = await setup();
    const long = Array.from(
      { length: 12 },
      (_, i) => `- [2026-10-10] 第${i}条自我认知，写得长一点才好触发缩水判定`,
    ).join('\n');
    await notes.write('PERSONA.md', `${long}\n`);

    const complete = vi.fn(async () => ({ content: '- 只剩这一条\nSUMMARY: 蒸馏完成', toolCalls: [] }));
    const dream = createDream({ llm: { complete }, notes, persona, every: 1 });

    dream.tick();
    await vi.waitFor(() => expect(complete).toHaveBeenCalledTimes(1));
    await vi.waitFor(() => expect(dream.summary()).toContain('蒸馏完成'));

    expect(dream.summary()).toContain('[缩水提示]');
    expect(await readFile(path.join(dir, 'PERSONA.md'), 'utf8')).toContain('只剩这一条');
    expect(await notes.gitLog('PERSONA.md', 5)).toContain('write: PERSONA.md');
  });
});
