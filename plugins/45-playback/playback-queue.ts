export type QueueEffect = { type: 'play'; sentence: string };

export class PlaybackQueue {
  private pending: string[] = [];
  private current: string | null = null;

  get waiting(): number {
    return this.pending.length;
  }

  enqueue(sentence: string): QueueEffect[] {
    this.pending.push(sentence);
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
