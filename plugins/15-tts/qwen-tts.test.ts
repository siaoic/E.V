import { describe, expect, it, vi } from 'vitest';
import { createQwenTtsSpeaker } from './qwen-tts.ts';

describe('createQwenTtsSpeaker', () => {
  it('句子作为 JSON POST 到 /speak，服务返回（播放结束）才结束', async () => {
    let resolveResponse: ((response: Response) => void) | undefined;
    const fetchMock = vi.fn<typeof fetch>(
      () => new Promise<Response>((resolve) => { resolveResponse = resolve; }),
    );
    const speaker = createQwenTtsSpeaker({ fetch: fetchMock, url: 'http://127.0.0.1:9999' });

    let done = false;
    const finished = speaker.speak('你好呀').then(() => { done = true; });
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalled());
    expect(done).toBe(false);
    resolveResponse!(new Response('ok', { status: 200 }));
    await finished;
    expect(done).toBe(true);

    expect(fetchMock).toHaveBeenCalledWith(
      'http://127.0.0.1:9999/speak',
      expect.objectContaining({
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ text: '你好呀' }),
      }),
    );
  });

  it('服务返回错误时抛出异常', async () => {
    const fetchMock = vi.fn<typeof fetch>(async () => new Response('boom', { status: 500 }));
    const speaker = createQwenTtsSpeaker({ fetch: fetchMock });

    await expect(speaker.speak('你好')).rejects.toThrow('TTS request failed: 500');
  });

  it('超过 15 秒未完成时中止并抛错，不永久挂起', async () => {
    vi.useFakeTimers();
    try {
      const fetchMock = vi.fn<typeof fetch>(
        (_url, init) =>
          new Promise<Response>((_resolve, reject) => {
            init?.signal?.addEventListener('abort', () => reject(new Error('aborted')));
          }),
      );
      const speaker = createQwenTtsSpeaker({ fetch: fetchMock, url: 'http://127.0.0.1:9999' });

      const pending = speaker.speak('你好');
      const expectation = expect(pending).rejects.toThrow('aborted');
      await vi.advanceTimersByTimeAsync(15000);
      await expectation;
    } finally {
      vi.useRealTimers();
    }
  });
});
