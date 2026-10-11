import { describe, expect, it } from 'vitest';
import { normalizeCommand } from './normalize.ts';

describe('normalizeCommand', () => {
  it('普通弹幕映射为观众弹幕事件，取出用户、文本与时间戳', () => {
    const command = {
      cmd: 'DANMU_MSG',
      info: [
        [0, 1, 25, 16777215, 1791548872188, 1791547601, 0, '746db7bb', 0, 0, 0, '', 0, '{}', '{}', {}],
        '小狗',
        [123456, '测试观众', 0, 0, 0, 0, 0, ''],
      ],
    };

    expect(normalizeCommand(command)).toEqual({
      type: 'danmaku',
      uid: 123456,
      uname: '测试观众',
      text: '小狗',
      timestamp: 1791548872188,
    });
  });

  it('醒目留言映射为 SC 事件，取出金额、留言与开始时间（毫秒）', () => {
    const command = {
      cmd: 'SUPER_CHAT_MESSAGE',
      data: {
        uid: 123456,
        price: 30,
        message: '测试留言',
        start_time: 1791548931,
        user_info: { uname: '测试观众' },
      },
    };

    expect(normalizeCommand(command)).toEqual({
      type: 'super-chat',
      uid: 123456,
      uname: '测试观众',
      text: '测试留言',
      price: 30,
      timestamp: 1791548931000,
    });
  });

  it('带参数后缀的弹幕命令 DANMU_MSG:4:0:2 与普通弹幕同样映射', () => {
    const command = {
      cmd: 'DANMU_MSG:4:0:2:2:2:0',
      info: [
        [0, 1, 25, 16777215, 1791548872188, 1791547601, 0, '746db7bb', 0, 0, 0, '', 0, '{}', '{}', {}],
        '小狗',
        [123456, '测试观众', 0, 0, 0, 0, 0, ''],
      ],
    };

    expect(normalizeCommand(command)).toEqual({
      type: 'danmaku',
      uid: 123456,
      uname: '测试观众',
      text: '小狗',
      timestamp: 1791548872188,
    });
  });

  it('心跳回复映射为心跳事件，人气值与到达时间随事件携带', () => {
    expect(normalizeCommand({ cmd: 'HEARTBEAT_REPLY', count: 48203 })).toEqual({
      type: 'heartbeat',
      count: 48203,
      timestamp: expect.any(Number),
    });
  });

  it('上舰 GUARD_BUY 映射为舰长事件：等级、月数、价格与开通时间', () => {
    const command = {
      cmd: 'GUARD_BUY',
      data: { uid: 123456, username: '舰长大人', guard_level: 3, num: 3, price: 138000, start_time: 1791548931 },
    };

    expect(normalizeCommand(command)).toEqual({
      type: 'guard',
      uid: 123456,
      uname: '舰长大人',
      guardLevel: 3,
      num: 3,
      price: 138000,
      timestamp: 1791548931000,
    });
  });

  it('上舰字段缺失或写成字符串时不炸：等级兜底 0、月数兜底 1', () => {
    expect(normalizeCommand({ cmd: 'GUARD_BUY', data: { uid: '7', username: '甲', guard_level: '1' } })).toEqual({
      type: 'guard',
      uid: 7,
      uname: '甲',
      guardLevel: 1,
      num: 1,
      price: 0,
      timestamp: 0,
    });
  });

  it('跨房弹幕 DANMU_MSG_MIRROR 不产生观众弹幕事件', () => {
    const command = {
      cmd: 'DANMU_MSG_MIRROR',
      info: [
        [0, 1, 25, 16777215, 1791548872188, 1791547601, 0, '746db7bb', 0, 0, 0, '', 0, '{}', '{}', {}],
        '小狗',
        [123456, '测试观众', 0, 0, 0, 0, 0, ''],
      ],
    };

    expect(normalizeCommand(command)).toBeNull();
  });
});
