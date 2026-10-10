import { describe, expect, it, vi } from 'vitest';
import { createLlmClient } from './client.ts';

describe('createLlmClient', () => {
  it('向 chat/completions 发送带鉴权的请求，并返回首条回复文本', async () => {
    const fetch = vi.fn(async () =>
      new Response(JSON.stringify({ choices: [{ message: { role: 'assistant', content: '你好呀' } }] }), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      }),
    );
    const client = createLlmClient({ baseUrl: 'https://example.test/v1', apiKey: 'KEY', model: 'm1', fetch });

    const reply = await client.complete([{ role: 'user', content: '嗨' }]);

    expect(reply).toEqual({ content: '你好呀', toolCalls: [] });
    const [url, init] = fetch.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe('https://example.test/v1/chat/completions');
    expect(init.method).toBe('POST');
    expect((init.headers as Record<string, string>).Authorization).toBe('Bearer KEY');
    expect(JSON.parse(init.body as string)).toEqual({
      model: 'm1',
      messages: [{ role: 'user', content: '嗨' }],
    });
  });

  it('把 extraBody 合并进请求体', async () => {
    const fetch = vi.fn(async () =>
      new Response(JSON.stringify({ choices: [{ message: { role: 'assistant', content: 'ok' } }] }), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      }),
    );
    const client = createLlmClient({
      baseUrl: 'https://example.test/v1',
      apiKey: 'KEY',
      model: 'm1',
      extraBody: { thinking: { type: 'disabled' } },
      fetch,
    });

    await client.complete([{ role: 'user', content: '嗨' }]);

    const [, init] = fetch.mock.calls[0] as unknown as [string, RequestInit];
    expect(JSON.parse(init.body as string)).toEqual({
      model: 'm1',
      messages: [{ role: 'user', content: '嗨' }],
      thinking: { type: 'disabled' },
    });
  });

  it('传入工具定义时带上 tools，并返回模型发起的工具调用', async () => {
    const toolCall = { id: 'call_1', type: 'function', function: { name: 'read_danmaku', arguments: '{}' } };
    const fetch = vi.fn(async () =>
      new Response(
        JSON.stringify({ choices: [{ message: { role: 'assistant', content: null, tool_calls: [toolCall] } }] }),
        { status: 200, headers: { 'content-type': 'application/json' } },
      ),
    );
    const client = createLlmClient({ baseUrl: 'https://example.test/v1', apiKey: 'KEY', model: 'm1', fetch });
    const tools = [
      {
        type: 'function' as const,
        function: { name: 'read_danmaku', description: '看弹幕', parameters: { type: 'object', properties: {} } },
      },
    ];

    const reply = await client.complete([{ role: 'user', content: '嗨' }], tools);

    expect(reply).toEqual({ content: null, toolCalls: [toolCall] });
    const [, init] = fetch.mock.calls[0] as unknown as [string, RequestInit];
    expect(JSON.parse(init.body as string).tools).toEqual(tools);
  });

  it('遇到 429 限流时退避后重发请求，成功即返回', async () => {
    vi.useFakeTimers();
    try {
      const fetch = vi
        .fn<() => Promise<Response>>()
        .mockResolvedValueOnce(new Response('limited', { status: 429 }))
        .mockResolvedValueOnce(
          new Response(JSON.stringify({ choices: [{ message: { role: 'assistant', content: 'ok' } }] }), {
            status: 200,
            headers: { 'content-type': 'application/json' },
          }),
        );
      const client = createLlmClient({ baseUrl: 'https://example.test/v1', apiKey: 'KEY', model: 'm1', fetch });

      const pending = client.complete([{ role: 'user', content: '嗨' }]);
      await vi.advanceTimersByTimeAsync(1000);

      expect(await pending).toEqual({ content: 'ok', toolCalls: [] });
      expect(fetch).toHaveBeenCalledTimes(2);
    } finally {
      vi.useRealTimers();
    }
  });

  it('请求超过 7 秒未响应时中止并按退避重试，最终成功', async () => {
    vi.useFakeTimers();
    try {
      let attempts = 0;
      const fetch = vi.fn((_url: URL | RequestInfo, init?: RequestInit) => {
        attempts += 1;
        if (attempts === 1) {
          return new Promise<Response>((_resolve, reject) => {
            init?.signal?.addEventListener('abort', () => reject(new Error('aborted')));
          });
        }
        return Promise.resolve(
          new Response(JSON.stringify({ choices: [{ message: { role: 'assistant', content: 'ok' } }] }), {
            status: 200,
            headers: { 'content-type': 'application/json' },
          }),
        );
      });
      const client = createLlmClient({ baseUrl: 'https://example.test/v1', apiKey: 'KEY', model: 'm1', fetch });

      const pending = client.complete([{ role: 'user', content: '嗨' }]);
      await vi.advanceTimersByTimeAsync(8000);

      expect(await pending).toEqual({ content: 'ok', toolCalls: [] });
      expect(attempts).toBe(2);
    } finally {
      vi.useRealTimers();
    }
  });

  it('连续超时耗尽重试次数后抛错', async () => {
    vi.useFakeTimers();
    try {
      const fetch = vi.fn((_url: URL | RequestInfo, init?: RequestInit) =>
        new Promise<Response>((_resolve, reject) => {
          init?.signal?.addEventListener('abort', () => reject(new Error('aborted')));
        }),
      );
      const client = createLlmClient({ baseUrl: 'https://example.test/v1', apiKey: 'KEY', model: 'm1', fetch });

      const pending = client.complete([{ role: 'user', content: '嗨' }]);
      const expectation = expect(pending).rejects.toThrow('aborted');
      await vi.advanceTimersByTimeAsync(40000);
      await expectation;
      expect(fetch).toHaveBeenCalledTimes(4);
    } finally {
      vi.useRealTimers();
    }
  });

  it('user 消息支持文本+图片混合内容，原样透传', async () => {
    const fetch = vi.fn(async () =>
      new Response(JSON.stringify({ choices: [{ message: { role: 'assistant', content: '一只猫' } }] }), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      }),
    );
    const client = createLlmClient({ baseUrl: 'https://example.test/v1', apiKey: 'KEY', model: 'm1', fetch });

    await client.complete([
      { role: 'user', content: [
        { type: 'text', text: '这是什么？' },
        { type: 'image_url', image_url: { url: 'data:image/png;base64,AAAA' } },
      ] },
    ]);

    const [, init] = fetch.mock.calls[0] as unknown as [string, RequestInit];
    const body = JSON.parse(init.body as string);
    expect(body.messages[0].content).toEqual([
      { type: 'text', text: '这是什么？' },
      { type: 'image_url', image_url: { url: 'data:image/png;base64,AAAA' } },
    ]);
  });

  it('describeImage 用视觉模型端点发图文消息，返回描述文本', async () => {
    const fetch = vi.fn(async () =>
      new Response(JSON.stringify({ choices: [{ message: { role: 'assistant', content: '一只橘猫趴在键盘上' } }] }), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      }),
    );
    const client = createLlmClient({
      baseUrl: 'https://example.test/v1', apiKey: 'KEY', model: 'm1',
      visionModel: 'glm-4v-flash', visionBaseUrl: 'https://vlm.example/v1', fetch,
    });

    const caption = await client.describeImage({ image: 'data:image/jpeg;base64,BBBB' });

    expect(caption).toBe('一只橘猫趴在键盘上');
    const [url, init] = fetch.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe('https://vlm.example/v1/chat/completions');
    const body = JSON.parse(init.body as string);
    expect(body.model).toBe('glm-4v-flash');
    expect(body.messages[0].content[1].image_url.url).toBe('data:image/jpeg;base64,BBBB');
  });

  it('接口返回非 2xx 时抛出包含状态码的错误', async () => {
    const fetch = vi.fn(async () => new Response('unauthorized', { status: 401 }));
    const client = createLlmClient({ baseUrl: 'https://example.test/v1', apiKey: 'BAD', model: 'm1', fetch });

    await expect(client.complete([{ role: 'user', content: '嗨' }])).rejects.toThrow('401');
  });
});
