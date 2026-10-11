export type QueueEffect = { type: 'play'; sentence: string };

export class PlaybackQueue {
  private pending: string[] = [];
  private current: string | null = null;

  get waiting(): number {
    return this.pending.length;
  }

  /** 排尾：保持入队顺序（旧语义）。当前由 enqueueFront 承担，这行保留备用 */
  enqueue(sentence: string): QueueEffect[] {
    this.pending.push(sentence);
    if (this.current !== null) return [];
    return this.advance();
  }

  /**
   * 队首插队：当前句一播完就先播它，已等待的句子往后让一位。
   * 设计上给高价值档（SC / 大额礼物 / 上舰）用；当前按用户决定（2026-10-11）一律走这里。
   * 代价：同一批排了多句时会倒序（后生成的先播）——要回退就换回 enqueue。
   */
  enqueueFront(sentence: string): QueueEffect[] {
    this.pending.unshift(sentence);
    if (this.current !== null) return [];
    return this.advance();
  }

  playbackCompleted(): QueueEffect[] {
    this.current = null;
    return this.advance();
  }

  private advance(): QueueEffect[] {
    const sentence = this.pending.shift();
    if (sentence === undefined) return [];
    this.current = sentence;
    return [{ type: 'play', sentence }];
  }
}
