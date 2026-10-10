import { afterAll, describe, expect, it } from 'vitest';
import { startViewerServer } from './server.ts';

const servers: Awaited<ReturnType<typeof startViewerServer>>[] = [];
afterAll(async () => {
  for (const server of servers) await server.close();
});

async function readUntil(reader: ReadableStreamDefaultReader<Uint8Array>, decoder: TextDecoder, needle: string): Promise<string> {
  let text = '';
  for (let i = 0; i < 10 && !text.includes(needle); i += 1) {
    const { value, done } = await reader.read();
    if (done) break;
    text += decoder.decode(value);
  }
  return text;
}

describe('viewer server', () => {
  it('根路径返回游戏提供的页面', async () => {
    const server = await startViewerServer({ page: '<p>sudoku</p>', port: 0 });
    servers.push(server);

    const response = await fetch(`${server.url}/`);
    expect(response.status).toBe(200);
    expect(await response.text()).toBe('<p>sudoku</p>');
  });

  it('连接 events 时推送初始状态，之后可广播事件', async () => {
    const server = await startViewerServer({
      page: '<p>x</p>',
      port: 0,
      onConnect: (client) => client.send('board', { filled: 37 }),
    });
    servers.push(server);

    const stream = await fetch(`${server.url}/events`);
    const reader = stream.body!.getReader();
    const decoder = new TextDecoder();

    const greeting = await readUntil(reader, decoder, 'event: board');
    expect(greeting).toContain(': connected');
    expect(greeting).toContain('event: board');
    expect(greeting).toContain('"filled":37');

    server.broadcaster.send('board', { filled: 38 });
    const broadcast = await readUntil(reader, decoder, '"filled":38');
    expect(broadcast).toContain('"filled":38');
    reader.cancel();
  });

  it('SSE 连接仍开着时 close() 也能返回', async () => {
    const server = await startViewerServer({ page: '<p>x</p>', port: 0 });
    servers.push(server);

    const stream = await fetch(`${server.url}/events`);
    await stream.body!.getReader().read();

    await expect(server.close()).resolves.toBeUndefined();
  });
});
