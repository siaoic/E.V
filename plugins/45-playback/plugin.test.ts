import { Context } from '@deepseek-ai/cordis';
import { describe, expect, it, vi } from 'vitest';
import { apply, type Speaker } from './plugin.ts';

const flush = () => new Promise((resolve) => setImmediate(resolve));

type Deferred = { promise: Promise<void>; resolve(): void };

function deferred(): Deferred {
  let resolve!: () => void;
  const promise = new Promise<void>((res) => {
    resolve = res;
  });
  return { promise, resolve };
}

const reply = (text: string) => ({ text, replyTo: [] });

describe('playback plugin', () => {
  it('回复逐句播报：开始时带上排队中的句数，完成后发出完成事件并接上下一句', async () => {
    const pending: Deferred[] = [];
    const speaker: Speaker = {
      speak: vi.fn<(sentence: string) => Promise<void>>(() => {
        const d = deferred();
        pending.push(d);
        return d.promise;
      }),
    };
    const root = new Context();
    const started = vi.fn();
    const completed = vi.fn();
    root.on('playback/started', started);
    root.on('playback/completed', completed);
    root.provide('speaker', speaker);
    await root.plugin(apply, {});

    root.emit('agent/reply', reply('第一句'));
    root.emit('agent/reply', reply('第二句'));
    await flush();

    expect(speaker.speak).toHaveBeenCalledTimes(1);
    expect(speaker.speak).toHaveBeenLastCalledWith('第一句');
    expect(started).toHaveBeenCalledWith({ sentence: '第一句', waiting: 0 });

    pending[0].resolve();
    await flush();

    expect(completed).toHaveBeenCalledWith({ sentence: '第一句' });
    expect(speaker.speak).toHaveBeenCalledTimes(2);
    expect(speaker.speak).toHaveBeenLastCalledWith('第二句');
  });

  it('某句播报失败时记录错误，并继续播放后面的句子', async () => {
    const speaker: Speaker = {
      speak: vi.fn<(sentence: string) => Promise<void>>(async (sentence) => {
        if (sentence === '坏句') throw new Error('tts down');
      }),
    };
    const root = new Context();
    const completed = vi.fn();
    root.on('playback/completed', completed);
    root.provide('speaker', speaker);
    await root.plugin(apply, {});

    root.emit('agent/reply', reply('坏句'));
    root.emit('agent/reply', reply('好句'));
    await flush();
    await flush();

    expect(speaker.speak).toHaveBeenLastCalledWith('好句');
    expect(completed).toHaveBeenCalledWith({ sentence: '好句' });
  });

  it('播报中到达的回复全部排队按序播完，不会被丢弃', async () => {
    const pending: Deferred[] = [];
    const speaker: Speaker = {
      speak: vi.fn<(sentence: string) => Promise<void>>(() => {
        const d = deferred();
        pending.push(d);
        return d.promise;
      }),
    };
    const root = new Context();
    root.provide('speaker', speaker);
    await root.plugin(apply, {});

    root.emit('agent/reply', reply('正在播'));
    root.emit('agent/reply', reply('待播一'));
    root.emit('agent/reply', reply('待播二'));
    await flush();
    pending[0].resolve();
    await flush();
    pending[1].resolve();
    await flush();
    pending[2].resolve();
    await flush();

    expect(speaker.speak).toHaveBeenCalledTimes(3);
    expect(speaker.speak).toHaveBeenNthCalledWith(2, '待播一');
    expect(speaker.speak).toHaveBeenNthCalledWith(3, '待播二');
  });
});
