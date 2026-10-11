import { appendFile, mkdir, readFile, writeFile } from 'node:fs/promises';
import path from 'node:path';
import type { Embedder } from './embedding.ts';
import { bigrams, cosine, dice } from '../text/similarity.ts';

export type MemoryRecord = { id: number; text: string; kind: string; createdAt: number; vector?: number[]; image?: string };

// bigrams / dice / cosine 搬到 src/text/similarity.ts：弹幕分层（src/llm/danmaku.ts）用同一套口径，
// 免得同一个阈值在两处各有一套"大概是什么感觉"。

// 文件工作区是人格权威：JSONL 追加存储。
// 检索优先语义向量（llama.cpp 嵌入），嵌入服务缺席或记录无向量时回退朴素 bigram 匹配；
// A_memorix 以后作为语义索引接入时，替换 recall 的实现即可，接口不变。
export class MemoryWorkspace {
  private records: MemoryRecord[] = [];
  private nextId = 1;
  private writer: Promise<void> = Promise.resolve();
  private embedder: Embedder | undefined;
  readonly file: string;

  private constructor(file: string) {
    this.file = file;
  }

  static async open(file: string): Promise<MemoryWorkspace> {
    const workspace = new MemoryWorkspace(file);
    try {
      const raw = await readFile(file, 'utf8');
      for (const line of raw.split('\n')) {
        if (!line.trim()) continue;
        try {
          workspace.records.push(JSON.parse(line) as MemoryRecord);
        } catch {
          // 跳过损坏的行，不中断加载
        }
      }
    } catch {
      // 文件不存在时从空开始
    }
    workspace.nextId = workspace.records.reduce((max, record) => Math.max(max, record.id), 0) + 1;
    return workspace;
  }

  setEmbedder(embedder: Embedder): void {
    this.embedder = embedder;
  }

  add(text: string, kind = 'note', image?: string): MemoryRecord {
    const record: MemoryRecord = { id: this.nextId, text, kind, createdAt: Date.now() };
    if (image) record.image = image;
    this.nextId += 1;
    this.records.push(record);
    this.writer = this.writer
      .then(async () => {
        // 写盘前先补向量；嵌入失败就存无向量记录，recall 时回退 bigram
        if (this.embedder) {
          const [vector] = (await this.embedder.embed([record.text])) ?? [];
          if (vector) record.vector = vector;
        }
        await mkdir(path.dirname(this.file), { recursive: true });
        await appendFile(this.file, `${JSON.stringify(record)}\n`, 'utf8');
      })
      .catch(() => {});
    return record;
  }

  /** 删除一条记忆并重写 JSONL；没有该 id 时返回 false */
  forget(id: number): boolean {
    const before = this.records.length;
    this.records = this.records.filter((record) => record.id !== id);
    if (this.records.length === before) return false;
    const snapshot = this.records.map((record) => JSON.stringify(record)).join('\n');
    this.writer = this.writer
      .then(async () => {
        await mkdir(path.dirname(this.file), { recursive: true });
        await writeFile(this.file, snapshot ? `${snapshot}\n` : '', 'utf8');
      })
      .catch(() => {});
    return true;
  }

  async flush(): Promise<void> {
    await this.writer;
  }

  size(): number {
    return this.records.length;
  }

  recent(limit: number): MemoryRecord[] {
    return this.records.slice(-limit);
  }

  async recall(query: string, limit = 5): Promise<MemoryRecord[]> {
    const recencyBonus = (index: number) => (index / Math.max(1, this.records.length)) * 0.1;
    let queryVector: number[] | undefined;
    if (this.embedder) {
      const [vector] = (await this.embedder.embed([query])) ?? [];
      queryVector = vector;
    }
    const scored = this.records
      .map((record, index) => {
        let score = 0;
        if (queryVector && record.vector && record.vector.length === queryVector.length) {
          // 维度必须一致（换嵌入模型后旧向量不作比较，回退 bigram）；
          // 余弦归一到 0–1，原始余弦 0.5 以下视为不相关
          const normalized = (cosine(queryVector, record.vector) + 1) / 2;
          score = normalized >= 0.75 ? normalized : 0;
        } else {
          const similarity = dice(bigrams(query), bigrams(record.text));
          score = similarity > 0.1 ? similarity : 0;
        }
        return { record, score: score > 0 ? score + recencyBonus(index) : 0 };
      })
      .filter((entry) => entry.score > 0)
      .sort((a, b) => b.score - a.score)
      .slice(0, limit);
    return scored.map((entry) => entry.record);
  }
}
