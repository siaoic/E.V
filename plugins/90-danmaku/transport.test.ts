import { describe, expect, it, vi } from 'vitest';
import { createWebSocketConnect } from './transport.ts';

class FakeWebSocket {
  binaryType = 'blob';
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: ArrayBuffer }) => void) | null = null;
  onclose: (() => void) | null = null;
  send = vi.fn<(data: Uint8Array) => void>();
  close = vi.fn<() => void>();
  constructor(public url: string) {}
}

describe('createWebSocketConnect', () => {
  it('连接指定地址，二进制收发，并转发 open、message 与 close 事件', () => {
    let created: FakeWebSocket | undefined;
    class RecordingWebSocket extends FakeWebSocket {
      constructor(url: string) {
        super(url);
        created = this;
      }
    }
    const handlers = { onOpen: vi.fn(), onMessage: vi.fn(), onClose: vi.fn() };

    const socket = createWebSocketConnect('wss://h1.chat.bilibili.com:443/sub', {
      WebSocketImpl: RecordingWebSocket as unknown as typeof WebSocket,
    })(handlers);

    const ws = created as FakeWebSocket;
    expect(ws.url).toBe('wss://h1.chat.bilibili.com:443/sub');
    expect(ws.binaryType).toBe('arraybuffer');

    ws.onopen?.();
    expect(handlers.onOpen).toHaveBeenCalledTimes(1);

    ws.onmessage?.({ data: new Uint8Array([1, 2, 3]).buffer });
    expect(handlers.onMessage).toHaveBeenCalledWith(Buffer.from([1, 2, 3]));

    ws.onclose?.();
    expect(handlers.onClose).toHaveBeenCalledTimes(1);

    socket.send(Buffer.from([9]));
    expect(ws.send).toHaveBeenCalledWith(Buffer.from([9]));

    socket.close();
    expect(ws.close).toHaveBeenCalledTimes(1);
  });
});
