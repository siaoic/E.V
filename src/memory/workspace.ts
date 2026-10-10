import { appendFile, mkdir, readFile, writeFile } from 'node:fs/promises';
import path from 'node:path';
import type { Embedder } from './embedding.ts';

export type MemoryRecord = { id: number; text: string; kind: string; createdAt: number; vector?: number[]; image?: string };

function bigrams(text: string): Set<string> {
  const set = new Set<string>();
  for (let i = 0; i < text.length - 1; i += 1) set.add(text.slice(i, i + 2));
  return set;
}

function dice(a: Set<string>, b: Set<string>): number {
  if (a.size === 0 || b.size === 0) return 0;
  let overlap = 0;
  for (const item of a) if (b.has(item)) overlap += 1;
  return (2 * overlap) / (a.size + b.size);
}

function cosine(a: number[], b: number[]): number {
  let dot = 0;
  let normA = 0;
  let normB = 0;
  for (let i = 0; i < a.length; i += 1) {
    dot += a[i] * b[i];
    normA += a[i] * a[i];
    normB += b[i] * b[i];
  }
  if (normA === 0 || normB === 0) return 0;
  return dot / (Math.sqrt(normA) * Math.sqrt(normB));
}

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
