import { createServer, type IncomingMessage, type ServerResponse } from 'node:http';

export type ViewerBroadcaster = { send(event: string, data: unknown): void };

export type ViewerServer = { url: string; close(): Promise<void>; broadcaster: ViewerBroadcaster };

// 观察服务是共用骨架：端口固定一个，页面由当前游戏提供；想换游戏就关掉当前进程再启动下一个
export function startViewerServer(
  options: {
    page: string;
    port?: number;
    onConnect?: (broadcaster: ViewerBroadcaster) => void;
  } = { page: '<p>no game</p>' },
): Promise<ViewerServer> {
  const port = options.port ?? 8787;
  const clients = new Set<ServerResponse>();

  const server = createServer((request: IncomingMessage, response: ServerResponse) => {
    if (request.url === '/events') {
      response.writeHead(200, {
        'content-type': 'text/event-stream',
        'cache-control': 'no-cache',
        connection: 'keep-alive',
      });
      response.write(': connected\n\n');
      clients.add(response);
      options.onConnect?.({
        send(event, data) {
          response.write(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`);
        },
      });
      request.on('close', () => clients.delete(response));
      return;
    }
    if (request.url === '/' || request.url === '/index.html') {
      response.writeHead(200, { 'content-type': 'text/html; charset=utf-8' });
      response.end(options.page);
      return;
    }
    response.writeHead(404);
    response.end();
  });

  return new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(port, '127.0.0.1', () => {
      const address = server.address();
      const actualPort = typeof address === 'object' && address !== null ? address.port : port;
      resolve({
        url: `http://127.0.0.1:${actualPort}`,
        broadcaster: {
          send(event, data) {
            for (const client of clients) client.write(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`);
          },
        },
        close: () =>
          new Promise((done) => {
            // SSE 连接会一直挂着，不主动断掉的话 server.close 的回调永远不来
            server.closeAllConnections();
            server.close(() => done());
          }),
      });
    });
  });
}
