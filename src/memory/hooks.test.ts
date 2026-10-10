import { mkdtemp, readFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { describe, expect, it } from 'vitest';
import { MemoryWorkspace } from './workspace.ts';
import { createMemoryToolkit, type MemoryToolkitHelpers } from './hooks.ts';
import { openNoteWorkspace } from './notes.ts';
import { openPersonaStore } from './persona.ts';
import { createViewerBook } from './viewers.ts';

const toolkit = async (helpers: MemoryToolkitHelpers = {}) =>
  createMemoryToolkit(await MemoryWorkspace.open(path.join(tmpdir(), `memory-hooks-${Math.random().toString(36).slice(2)}.jsonl`)), helpers);
const vision = (caption: string | Error): MemoryToolkitHelpers => ({
  describeImage: async () => {
    if (caption instanceof Error) throw caption;
    return caption;
  },
});

describe('memory toolkit', () => {
  it('memory_save 存入并返回已记住，text 和 image 都缺时提示', async () => {
    const memory = await toolkit();

    expect(await memory.run('memory_save', '{"text":"讲过修打印机的故事","kind":"story"}')).toBe('（已记住：讲过修打印机的故事）');
    expect(await memory.run('memory_save', '{"text":"猫猫图","image":"C:/pics/cat.png"}')).toBe('（已记住：猫猫图）');
    expect(await memory.run('memory_save', '{}')).toBe('（text 和 image 至少给一个）');
    expect(await memory.run('memory_save', 'not json')).toBe('（text 和 image 至少给一个）');
  });

  it('带图不带文字且没有看图能力时，提示补描述，不落盘', async () => {
    const memory = await toolkit();

    expect(await memory.run('memory_save', '{"image":"C:/pics/x.png"}')).toContain('请自己补一句描述');
    expect(memory.recentDigest(1)).toBe('');
  });

  it('带图不带文字时自动看图生成描述并落盘', async () => {
    const memory = await toolkit(vision('一只橘猫趴在键盘上'));

    expect(await memory.run('memory_save', '{"image":"C:/pics/cat.png"}')).toBe('（已记住：一只橘猫趴在键盘上）');
    expect(memory.recentDigest(1)).toContain('[图] C:/pics/cat.png — 一只橘猫趴在键盘上');
  });

  it('看图失败时不落盘，提示原因', async () => {
    const memory = await toolkit(vision(new Error('看图超时')));

    expect(await memory.run('memory_save', '{"image":"C:/pics/x.png"}')).toContain('看图失败');
    expect(memory.recentDigest(1)).toBe('');
  });

  it('memory_recall 返回相关记忆，图片记忆带 [图] 标记', async () => {
    const memory = await toolkit();
    await memory.run('memory_save', '{"text":"讲过修打印机的故事"}');
    await memory.run('memory_save', '{"text":"观众发的猫猫图","image":"C:/pics/cat.png"}');

    const found = await memory.run('memory_recall', '{"query":"打印机"}');
    expect(found).toContain('讲过修打印机的故事');

    const imageMemory = await memory.run('memory_recall', '{"query":"猫猫图"}');
    expect(imageMemory).toMatch(/\[#\d+\] \[图\] C:\/pics\/cat\.png — 观众发的猫猫图/);

    expect(await memory.run('memory_recall', '{"query":"完全无关的词"}')).toBe('（没有相关记忆）');
    expect(await memory.run('memory_recall', '{}')).toBe('（query 必须是关键词）');
  });

  it('recentDigest 空时返回空串，有记忆时列出摘要', async () => {
    const memory = await toolkit();

    expect(memory.recentDigest(8)).toBe('');

    await memory.run('memory_save', '{"text":"讲过修打印机的故事"}');
    await memory.run('memory_save', '{"text":"观众发的猫猫图","image":"C:/pics/cat.png"}');
    const digest = memory.recentDigest(8);

    expect(digest).toContain('别重复');
    expect(digest).toContain('讲过修打印机的故事');
    expect(digest).toContain('[图] C:/pics/cat.png — 观众发的猫猫图');
  });

  it('persona_update 追加进人格笔记，缺人格工作区时提示', async () => {
    const memory = await toolkit();
    expect(await memory.run('persona_update', '{"note":"我喜欢先嘴硬"}')).toBe('（当前没有人格工作区）');

    const persona = await openPersonaStore(await mkdtemp(path.join(tmpdir(), 'persona-')));
    const withPersona = await toolkit({ persona });
    expect(await withPersona.run('persona_update', '{"note":"我喜欢先嘴硬"}')).toBe('（已沉淀：我喜欢先嘴硬）');
    expect(persona.prompt()).toContain('我喜欢先嘴硬');
    expect(await withPersona.run('persona_update', '{}')).toBe('（note 必须是一句自我认知）');
  });

  it('context 开场带别重复摘要，普通窗口按话题召回且不含摘要', async () => {
    const memory = await toolkit();
    await memory.run('memory_save', '{"text":"讲过修打印机的故事"}');
    await memory.run('memory_save', '{"text":"聊过夏天的凉面"}');

    const opening = await memory.context({ opening: true });
    expect(opening).toContain('别重复');
    expect(opening).toContain('讲过修打印机的故事');

    const related = await memory.context({ topic: '凉面', lastSpoken: '好饿', opening: false });
    expect(related).toContain('聊过夏天的凉面');
    expect(related).not.toContain('别重复');

    expect(await memory.context({ opening: false })).toBe('');
  });

  it('memory_forget 删掉指定记忆，recall 行首带 [#id] 供寻址', async () => {
    const memory = await toolkit();
    await memory.run('memory_save', '{"text":"讲过修打印机的故事"}');
    await memory.run('memory_save', '{"text":"聊过夏天的凉面"}');

    const found = await memory.run('memory_recall', '{"query":"凉面"}');
    expect(found).toMatch(/\[#2\] 聊过夏天的凉面/);

    expect(await memory.run('memory_forget', '{"id":2}')).toBe('（已忘掉 #2）');
    expect(await memory.run('memory_recall', '{"query":"凉面"}')).toBe('（没有相关记忆）');
    expect(await memory.run('memory_forget', '{"id":2}')).toBe('（没有 #2 这条记忆）');
    expect(await memory.run('memory_forget', '{}')).toBe('（id 必须是 memory_recall 结果里的编号）');
  });

  it('persona_revise 修订或收回自我认知，未命中提示', async () => {
    const persona = await openPersonaStore(await mkdtemp(path.join(tmpdir(), 'persona-')));
    const memory = await toolkit({ persona });
    await memory.run('persona_update', '{"note":"我喜欢先嘴硬"}');

    const line = persona.prompt().match(/- \[\d{4}-\d{2}-\d{2}\] 我喜欢先嘴硬/)?.[0];
    expect(line).toBeTruthy();
    expect(
      await memory.run('persona_revise', JSON.stringify({ match: line, replace: '- 我现在会直接承认' })),
    ).toBe('（已修订）');
    expect(persona.prompt()).toContain('- 我现在会直接承认');

    expect(await memory.run('persona_revise', JSON.stringify({ match: '- 我现在会直接承认', replace: '' }))).toBe(
      '（已收回这条认知）',
    );
    expect(persona.prompt()).not.toContain('我现在会直接承认');

    expect(await memory.run('persona_revise', JSON.stringify({ match: '不存在的一行' }))).toContain('没找到这一行');
    expect(await memory.run('persona_revise', '{}')).toBe('（match 必须是要改的那一行的原文）');
  });

  it('memory_save 带 viewer 记入观众档案，recall_viewer 调出，viewerLines 取首行', async () => {
    const notes = await openNoteWorkspace(await mkdtemp(path.join(tmpdir(), 'notes-viewers-')));
    const viewers = createViewerBook(notes);
    const memory = await toolkit({ viewers });

    expect(await memory.run('memory_save', '{"text":"爱发猫图","kind":"viewer","viewer":7}')).toBe(
      '（已记进 #7 的观众档案：爱发猫图）',
    );
    expect(await memory.run('recall_viewer', '{"uid":7}')).toContain('爱发猫图');
    expect(await memory.run('recall_viewer', '{"uid":8}')).toBe('（还没有 #8 的档案）');

    const lines = await memory.viewerLines([7, 8, 8]);
    expect(lines).toMatch(/\[#7\] .+爱发猫图/);
    expect(lines).not.toContain('[#8]');

    expect(await memory.run('memory_save', '{"text":"x","kind":"fact","viewer":7}')).toBe(
      '（记观众档案要把 kind 设为 viewer）',
    );
    await memory.run('memory_save', '{"text":"观众群像","kind":"viewer"}');
    expect(memory.recentDigest(1)).toContain('观众群像');
    expect(await memory.viewerLines([])).toBe('');
  });

  it('没有观众档案服务时给出提示', async () => {
    const memory = await toolkit();
    expect(await memory.run('recall_viewer', '{"uid":7}')).toBe('（当前没有观众档案）');
    expect(await memory.viewerLines([7])).toBe('');
  });

  it('未知工具返回未知工具', async () => {
    const memory = await toolkit();

    expect(await memory.run('memory_dance', '{}')).toBe('（未知工具）');
  });
});
