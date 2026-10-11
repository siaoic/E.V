// 文本相似度的共享纯函数。
// 记忆检索（src/memory/workspace.ts）与弹幕分层（src/llm/danmaku.ts）共用同一套口径：
// 同一个阈值（比如 0.25）只该有一种"大概是什么感觉"的解释，两处各写一份迟早会打架。

/** 相邻两字组集合。中文没有空格，bigram 是免分词的最小语义单位 */
export function bigrams(text: string): Set<string> {
  const set = new Set<string>();
  for (let i = 0; i < text.length - 1; i += 1) set.add(text.slice(i, i + 2));
  return set;
}

/** 交集大小。短文本要额外要求它，否则一个二字组就能把 Dice 顶过阈值 */
export function overlap(a: Set<string>, b: Set<string>): number {
  let shared = 0;
  for (const item of a) if (b.has(item)) shared += 1;
  return shared;
}

/** Dice 系数：2|A∩B| / (|A| + |B|)。对长度不敏感，比 Jaccard 对短文本宽容 */
export function dice(a: Set<string>, b: Set<string>): number {
  if (a.size === 0 || b.size === 0) return 0;
  return (2 * overlap(a, b)) / (a.size + b.size);
}

/** 余弦相似度（向量检索用） */
export function cosine(a: number[], b: number[]): number {
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
