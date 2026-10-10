import { appendFile, readFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { describe, expect, it } from 'vitest';
import { MemoryWorkspace } from './workspace.ts';
import type { Embedder } from './embedding.ts';

const newFile = () => path.join(tmpdir(), `memory-test-${Math.random().toString(36).slice(2)}`, 'memory.jsonl');

const fakeEmbedder = (table: Record<string, number[]>): Embedder => ({
  async embed(texts) {
    const vectors = texts.map((text) => table[text]);
    return vectors.every(Boolean) ? vectors : null;
  },
});

describe('MemoryWorkspace', () => {
  it('add 追加写入 JSONL，重开能完整加载', async () => {
    const file = newFile();
    const first = await MemoryWorkspace.open(file);
    first.add('讲过帮大爷修打印机的故事', 'story');
    first.add('喜欢在深夜打游戏', 'fact');
    await first.flush();

    const raw = await readFile(file, 'utf8');
    expect(raw.split('\n').filter(Boolean)).toHaveLength(2);

    const second = await MemoryWorkspace.open(file);
    expect(second.size()).toBe(2);
    expect(second.recent(2).map((record) => record.text)).toEqual(['讲过帮大爷修打印机的故事', '喜欢在深夜打游戏']);
  });

  it('重开后 id 继续递增，不与旧记录冲突', async () => {
    const file = newFile();
    const first = await MemoryWorkspace.open(file);
    first.add('第一条', 'story');
    await first.flush();

    const second = await MemoryWorkspace.open(file);
    const record = second.add('第二条', 'story');

    expect(record.id).toBe(2);
  });

  it('注入嵌入器后，记录落盘带向量', async () => {
    const file = newFile();
    const workspace = await MemoryWorkspace.open(file);
    workspace.setEmbedder(fakeEmbedder({ 讲过打印机的故事: [1, 0] }));
    workspace.add('讲过打印机的故事', 'story');
    await workspace.flush();

    const raw = await readFile(file, 'utf8');
    expect(JSON.parse(raw.trim())).toMatchObject({ text: '讲过打印机的故事', vector: [1, 0] });
  });

  it('recall 用语义向量：表述不同但语义相关的记录能被召回', async () => {
    const workspace = await MemoryWorkspace.open(newFile());
    workspace.setEmbedder(
      fakeEmbedder({
        帮大爷修过打印机: [1, 0.9, 0],
        打印机坏了拿去修: [0.9, 1, 0],
        参加过程序设计比赛: [0, 0, 1],
      }),
    );
    workspace.add('帮大爷修过打印机', 'story');
    workspace.add('参加过程序设计比赛', 'story');

    const found = await workspace.recall('打印机坏了拿去修', 2);

    expect(found.map((record) => record.text)).toEqual(['帮大爷修过打印机']);
  });

  it('查询嵌入失败时整条回退 bigram', async () => {
    const workspace = await MemoryWorkspace.open(newFile());
    workspace.setEmbedder({ embed: async () => null });
    workspace.add('讲过打印机的故事', 'story');

    const found = await workspace.recall('打印机', 2);

    expect(found.map((record) => record.text)).toEqual(['讲过打印机的故事']);
  });

  it('维度不一致的旧向量不作比较，回退 bigram（换嵌入模型的兼容守卫）', async () => {
    const file = newFile();
    const { mkdir, writeFile } = await import('node:fs/promises');
    await mkdir(path.dirname(file), { recursive: true });
    const old = JSON.stringify({ id: 1, text: '换模型前的旧记录', kind: 'story', createdAt: 1, vector: [0, 1, 0, 0, 0, 0, 0, 0, 0] });
    const current = JSON.stringify({ id: 2, text: '讲过打印机的故事', kind: 'story', createdAt: 2, vector: [1, 0, 0] });
    await writeFile(file, `${old}\n${current}\n`);

    const reloaded = await MemoryWorkspace.open(file);
    reloaded.setEmbedder(fakeEmbedder({ 打印机: [1, 0, 0] }));

    const found = await reloaded.recall('打印机', 5);

    expect(found.map((record) => record.text)).toEqual(['讲过打印机的故事']);
  });

  it('recall 查不到时返回空', async () => {
    const workspace = await MemoryWorkspace.open(newFile());
    workspace.add('讲过打印机的故事', 'story');

    expect(await workspace.recall('量子力学')).toEqual([]);
  });

  it('带图片路径的记忆按字段保存', async () => {
    const workspace = await MemoryWorkspace.open(newFile());
    workspace.add('观众送的猫猫图', 'viewer', 'C:/pics/cat.png');

    expect(workspace.recent(1)[0]).toMatchObject({ text: '观众送的猫猫图', image: 'C:/pics/cat.png' });
  });

  it('损坏的行跳过不中断加载', async () => {
    const file = newFile();
    const first = await MemoryWorkspace.open(file);
    first.add('好记录', 'fact');
    await first.flush();
    await appendFile(file, '{broken json\n');

    const second = await MemoryWorkspace.open(file);
    expect(second.size()).toBe(1);
    expect(second.recent(1)[0].text).toBe('好记录');
  });

  it('forget 删除一条并重写 JSONL，重开后不再出现；id 不存在返回 false', async () => {
    const file = newFile();
    const first = await MemoryWorkspace.open(file);
    first.add('第一条', 'story');
    first.add('第二条', 'story');
    await first.flush();

    expect(first.forget(1)).toBe(true);
    expect(first.forget(1)).toBe(false);
    await first.flush();

    const raw = await readFile(file, 'utf8');
    expect(raw.split('\n').filter(Boolean)).toHaveLength(1);
    expect(raw).not.toContain('第一条');

    const second = await MemoryWorkspace.open(file);
    expect(second.size()).toBe(1);
    expect(second.recent(1)[0].text).toBe('第二条');
  });

  it('forget 清空全部时文件重写为空', async () => {
    const file = newFile();
    const workspace = await MemoryWorkspace.open(file);
    workspace.add('唯一一条', 'fact');
    await workspace.flush();

    expect(workspace.forget(1)).toBe(true);
    await workspace.flush();

    const raw = await readFile(file, 'utf8');
    expect(raw).toBe('');
    expect(workspace.size()).toBe(0);
  });
});
