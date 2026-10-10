import type { DanmakuSocketHandlers, DanmakuSocket } from './source.ts';

export function createWebSocketConnect(
  url: string,
  { WebSocketImpl = globalThis.WebSocket }: { WebSocketImpl?: typeof WebSocket } = {},
): (handlers: DanmakuSocketHandlers) => DanmakuSocket {
  return (handlers) => {
    const ws = new WebSocketImpl(url);
    ws.binaryType = 'arraybuffer';
    ws.onopen = () => handlers.onOpen();
    ws.onmessage = (event) => handlers.onMessage(Buffer.from(event.data as ArrayBuffer));
    ws.onclose = () => handlers.onClose();
    return {
      send: (packet) => ws.send(packet as Uint8Array<ArrayBuffer>),
      close: () => ws.close(),
    };
  };
}
