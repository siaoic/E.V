import { describe, expect, it } from 'vitest';
import { bigrams, cosine, dice, overlap } from './similarity.ts';

describe('文本相似度（共享纯函数）', () => {
  it('bigrams 取相邻两字组，中文免分词；单字无二字组', () => {
    expect([...bigrams('我家猫')]).toEqual(['我家', '家猫']);
    expect([...bigrams('a')]).toEqual([]);
  });

  it('dice：相同为 1、无重合为 0，部分重合按 2|A∩B|/(|A|+|B|) 算', () => {
    expect(dice(bigrams('猫又上桌'), bigrams('猫又上桌'))).toBe(1);
    expect(dice(bigrams('今天唱什么歌'), bigrams('我家猫又上桌了'))).toBe(0);
    // 6 个二字组 vs 5 个，重合 4 个
    expect(dice(bigrams('我家猫又上桌了'), bigrams('猫又上桌了吗'))).toBeCloseTo(8 / 11, 5);
  });

  it('overlap 单独可查：短文本虚高要靠它兜底', () => {
    expect(overlap(bigrams('我家猫又上桌了'), bigrams('上桌'))).toBe(1);
    expect(overlap(bigrams('我家猫又上桌了'), bigrams('猫又上桌啦'))).toBe(3);
  });

  it('cosine 与向量检索同口径，零向量返回 0', () => {
    expect(cosine([1, 0], [1, 0])).toBe(1);
    expect(cosine([1, 0], [0, 1])).toBe(0);
    expect(cosine([0, 0], [1, 0])).toBe(0);
  });
});
