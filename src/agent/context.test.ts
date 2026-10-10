import { describe, expect, it } from 'vitest';
import { buildContext } from './context.ts';

describe('buildContext', () => {
  it('宪法种子在系统消息头部，纪律与游戏规则紧随其后', () => {
    const messages = buildContext({
      personaPrompt: '（种子）',
      disciplinePrompt: '纪律。',
      gamePrompt: '游戏规则。',
      windowPrompt: '（开头）',
    });

    expect(messages[0]).toEqual({ role: 'system', content: '（种子）\n\n纪律。游戏规则。' });
    expect(messages.at(-1)).toEqual({ role: 'user', content: '（开头）' });
  });

  it('无人格时系统消息就是纪律本身，游戏规则为空时不留尾巴', () => {
    const messages = buildContext({ disciplinePrompt: '纪律。', windowPrompt: 'x' });

    expect(messages[0]).toEqual({ role: 'system', content: '纪律。' });
  });

  it('spoken 逐句成 assistant 历史，盘面/记忆/窗口提示按序进 user', () => {
    const messages = buildContext({
      disciplinePrompt: 'd',
      spoken: ['一', '二'],
      gameDescribe: '（盘面）',
      memoryContext: '（记忆）',
      windowPrompt: '（讲）',
    });

    expect(messages).toEqual([
      { role: 'system', content: 'd' },
      { role: 'assistant', content: '一' },
      { role: 'assistant', content: '二' },
      { role: 'user', content: '（盘面）\n（记忆）\n（讲）' },
    ]);
  });

  it('有游戏但描述为空时保留占位（与原行为一致），无盘面记忆时 user 就是窗口提示', () => {
    const withEmptyDescribe = buildContext({ disciplinePrompt: 'd', gameDescribe: '', windowPrompt: '（讲）' });
    expect(withEmptyDescribe.at(-1)).toEqual({ role: 'user', content: '\n（讲）' });

    const bare = buildContext({ disciplinePrompt: 'd', windowPrompt: '（接着讲）' });
    expect(bare.at(-1)).toEqual({ role: 'user', content: '（接着讲）' });
  });
});
