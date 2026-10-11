import { describe, expect, it } from 'vitest';
import { classifyWindowItems, isNoise, isQuestion, type DanmakuItem } from './danmaku.ts';

const danmaku = (text: string, uid = 1): DanmakuItem => ({ type: 'danmaku', uid, uname: '观众', text, timestamp: 0 });
const superChat = (): DanmakuItem => ({ type: 'super-chat', uid: 9, uname: '富哥', text: '唱一首', price: 30, timestamp: 0 });
const gift = (): DanmakuItem => ({
  type: 'gift',
  uid: 2,
  uname: '土豪',
  giftName: '火箭',
  num: 1,
  price: 1000,
  coinType: 'gold',
  timestamp: 0,
});
const guard = (): DanmakuItem => ({ type: 'guard', uid: 7, uname: '阿紫', guardLevel: 3, num: 1, price: 138000, timestamp: 0 });

const texts = (items: DanmakuItem[]): string[] => items.map((item) => (item.type === 'danmaku' ? item.text : item.type));

describe('分层判定的小工具', () => {
  it('疑问结尾：尾部的句末标点先去掉再判断', () => {
    expect(isQuestion('在吗')).toBe(true);
    expect(isQuestion('今天唱什么歌？')).toBe(true);
    expect(isQuestion('吃了吗。')).toBe(true);
    expect(isQuestion('在吗？')).toBe(true);
    expect(isQuestion('我刚唱完一首')).toBe(false);
    expect(isQuestion('？？')).toBe(false);
  });

  it('纯表情/纯标点：≤2 字符且不含字母数字', () => {
    expect(isNoise('……')).toBe(true);
    expect(isNoise('😂')).toBe(true);
    expect(isNoise('[doge]')).toBe(false);
    expect(isNoise('哈哈哈')).toBe(false);
  });
});

describe('弹幕分层（只判断相关）', () => {
  it('与可见发言相关 → 逐条；不相关 → 只计数', () => {
    const items = [danmaku('猫又上桌啦', 1), danmaku('今天天气不错', 2), danmaku('666', 3)];

    const { listed, hidden } = classifyWindowItems(items, ['我家猫又上桌了']);

    expect(texts(listed)).toEqual(['猫又上桌啦']);
    expect(hidden).toBe(2);
  });

  it('短文本补丁：只重合一个二字组不算相关', () => {
    const { listed, hidden } = classifyWindowItems([danmaku('上桌')], ['我家猫又上桌了']);

    expect(listed).toEqual([]);
    expect(hidden).toBe(1);
  });

  it('疑问结尾的弹幕即使无重合也逐条', () => {
    expect(texts(classifyWindowItems([danmaku('在吗')], []).listed)).toEqual(['在吗']);
    expect(classifyWindowItems([danmaku('在家里')], []).listed).toEqual([]);
  });

  it('她上一句是问句时，接着来的弹幕视为相关（对话邻接）', () => {
    const items = [danmaku('夜曲'), danmaku('随便')];

    expect(texts(classifyWindowItems(items, ['你们今天想听什么歌？']).listed)).toEqual(['夜曲', '随便']);
    expect(classifyWindowItems(items, ['我刚唱完一首']).listed).toEqual([]);
  });

  it('SC / 礼物 / 上舰恒逐条，不参与相关度判定', () => {
    const { listed, hidden } = classifyWindowItems([superChat(), gift(), guard()], []);

    expect(texts(listed)).toEqual(['super-chat', 'gift', 'guard']);
    expect(hidden).toBe(0);
  });

  it('非弹幕不占弹幕额度：10 条相关弹幕不会把上舰挤掉', () => {
    const items = [...Array.from({ length: 10 }, (_, i) => danmaku(`猫又上桌了我家${i}`, i + 1)), guard()];

    const { listed, hidden } = classifyWindowItems(items, ['猫又上桌了我家']);

    expect(listed).toHaveLength(11);
    expect(listed[0].type).toBe('guard');
    expect(hidden).toBe(0);
  });

  it('同 uid 同文本复读只留第一条', () => {
    const { listed, hidden } = classifyWindowItems([danmaku('猫又上桌啦', 5), danmaku('猫又上桌啦', 5)], ['我家猫又上桌了']);

    expect(texts(listed)).toEqual(['猫又上桌啦']);
    expect(hidden).toBe(1);
  });

  it('纯表情与纯标点只计数，不进上下文', () => {
    const { listed, hidden } = classifyWindowItems([danmaku('……'), danmaku('😂')], ['我家猫又上桌了']);

    expect(listed).toEqual([]);
    expect(hidden).toBe(2);
  });

  it('相关弹幕只呈现最新 10 条，多出来的计入聚合', () => {
    const items = Array.from({ length: 11 }, (_, i) => danmaku(`猫又上桌了我家${i}`, i + 1));

    const { listed, hidden } = classifyWindowItems(items, ['猫又上桌了我家']);

    expect(listed).toHaveLength(10);
    expect(texts(listed)[0]).toBe('猫又上桌了我家1');
    expect(texts(listed).at(-1)).toBe('猫又上桌了我家10');
    expect(hidden).toBe(1);
  });

  it('空输入不炸', () => {
    expect(classifyWindowItems([], ['随便说点什么'])).toEqual({ listed: [], hidden: 0 });
  });
});
