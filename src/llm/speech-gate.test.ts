import { describe, expect, it } from 'vitest';
import { SpeechGate } from './speech-gate.ts';

describe('SpeechGate', () => {
  it('与近期发言规范化后完全相同的发言意图被拒绝', () => {
    const gate = new SpeechGate();

    const result = gate.check({ text: '今天天气真好！' }, ['今天天气真好。']);

    expect(result).toEqual({ accepted: false, reason: 'repetition' });
  });
});
