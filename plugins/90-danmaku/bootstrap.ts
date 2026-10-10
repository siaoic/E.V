import { fetchBuvid3, fetchDanmuServer } from './server.ts';
import { createWebSocketConnect } from './transport.ts';

export type ResolvedConnect = { connect: ReturnType<typeof createWebSocketConnect>; buvid: string; token: string };

// 插件自举：插件配置里没有 connect（函数不可序列化）时，到这里现场解析
export async function resolveConnect(options: {
  roomId: number;
  buvid?: string;
  fetch?: typeof globalThis.fetch;
}): Promise<ResolvedConnect> {
  const buvid = options.buvid ?? (await fetchBuvid3(options.fetch));
  const server = await fetchDanmuServer({ roomId: options.roomId, buvid, fetch: options.fetch });
  const host = server.hosts[0];
  return {
    connect: createWebSocketConnect(`wss://${host.host}:${host.wssPort}/sub`),
    buvid,
    token: server.token,
  };
}
