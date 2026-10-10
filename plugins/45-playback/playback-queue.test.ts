import { describe, expect, it } from 'vitest';
import { PlaybackQueue } from './playback-queue.ts';

describe('PlaybackQueue', () => {
  it('空队列入队即开始播放', () => {
    const queue = new PlaybackQueue();

    expect(queue.enqueue('第一句')).toEqual([{ type: 'play', sentence: '第一句' }]);
  });

  it('播报完成接上队列里的下一句，正在播的期间新入队的等待播放', () => {
    const queue = new PlaybackQueue();
    queue.enqueue('第一句');
    expect(queue.enqueue('第二句')).toEqual([]);

    expect(queue.playbackCompleted()).toEqual([{ type: 'play', sentence: '第二句' }]);
  });

  it('队列为空时播报完成，链条停止', () => {
    const queue = new PlaybackQueue();
    queue.enqueue('最后一句');

    expect(queue.playbackCompleted()).toEqual([]);
  });

  it('待播内容不丢弃，全部按入队顺序播完', () => {
    const queue = new PlaybackQueue();
    queue.enqueue('正在播');
    for (let i = 1; i <= 6; i += 1) queue.enqueue(`待播${i}`);

    const played: string[] = [];
    for (let effects = queue.playbackCompleted(); effects.length > 0; effects = queue.playbackCompleted()) {
      played.push(effects[0].sentence);
    }
    expect(played).toEqual(['待播1', '待播2', '待播3', '待播4', '待播5', '待播6']);
  });

  it('waiting 报告正在播之外还在等待的句子数', () => {
    const queue = new PlaybackQueue();
    queue.enqueue('正在播');
    expect(queue.waiting).toBe(0);

    queue.enqueue('待播一');
    queue.enqueue('待播二');
    expect(queue.waiting).toBe(2);

    queue.playbackCompleted();
    expect(queue.waiting).toBe(1);
  });
});
