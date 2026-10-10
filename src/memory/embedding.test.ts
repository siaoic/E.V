import { describe, expect, it, vi } from 'vitest';
import { createEmbedder } from './embedding.ts';

const embeddingResponse = (vectors: number[][]) =>
  new Response(JSON.stringify({ data: vectors.map((embedding) => ({ embedding })) }), {
    status: 200,
    headers: { 'content-type': 'application/json' },
  });

describe('createEmbedder', () => {
  it('POST /v1/embeddings 并返回向量', async () => {
    const fetchMock = vi.fn(async () => embeddingResponse([[1, 2], [3, 4]]));
    const embedder = createEmbedder({ url: 'http://127.0.0.1:8761', model: 'm', fetch: fetchMock as unknown as typeof fetch });

    const vectors = await embedder.embed(['甲', '乙']);

    expect(vectors).toEqual([[1, 2], [3, 4]]);
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe('http://127.0.0.1:8761/v1/embeddings');
    expect(JSON.parse(init.body as string)).toEqual({ model: 'm', input: ['甲', '乙'] });
  });

  it('空输入直接返回空数组，不发请求', async () => {
    const fetchMock = vi.fn();
    const embedder = createEmbedder({ url: 'http://x', model: 'm', fetch: fetchMock as unknown as typeof fetch });

    expect(await embedder.embed([])).toEqual([]);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('服务不可达、非 2xx、向量数量不符时都返回 null', async () => {
    const unreachable = createEmbedder({ url: 'http://x', model: 'm', fetch: vi.fn(async () => { throw new Error('down'); }) as unknown as typeof fetch });
    expect(await unreachable.embed(['a'])).toBeNull();

    const serverError = createEmbedder({ url: 'http://x', model: 'm', fetch: vi.fn(async () => new Response('err', { status: 500 })) as unknown as typeof fetch });
    expect(await serverError.embed(['a'])).toBeNull();

    const mismatched = createEmbedder({ url: 'http://x', model: 'm', fetch: vi.fn(async () => embeddingResponse([[1]])) as unknown as typeof fetch });
    expect(await mismatched.embed(['a', 'b'])).toBeNull();
  });

  it('超过超时时间未响应时中止并返回 null', async () => {
    vi.useFakeTimers();
    try {
      const fetchMock = vi.fn((_url: URL | RequestInfo, init?: RequestInit) =>
        new Promise<Response>((_resolve, reject) => {
          init?.signal?.addEventListener('abort', () => reject(new Error('aborted')));
        }),
      );
      const embedder = createEmbedder({ url: 'http://x', model: 'm', fetch: fetchMock as unknown as typeof fetch, timeoutMs: 7000 });

      const pending = embedder.embed(['a']);
      const expectation = expect(pending).resolves.toBeNull();
      await vi.advanceTimersByTimeAsync(7000);
      await expectation;
    } finally {
      vi.useRealTimers();
    }
  });
});
