import { Context } from '@deepseek-ai/cordis';
import { apply as danmakuApply } from './plugin.ts';
import { fetchBuvid3, fetchDanmuServer } from './server.ts';
import { createWebSocketConnect } from './transport.ts';

const roomId = Number(process.argv[2] ?? 22625027);
const seconds = Number(process.argv[3] ?? 7);

const buvid = await fetchBuvid3();
const server = await fetchDanmuServer({ roomId, buvid });
const host = server.hosts[0];
console.log(`room=${roomId} host=${host.host}:${host.wssPort} buvid3=${buvid.slice(0, 8)}…`);

const root = new Context();
root.on('world/live-event', (event) => console.log(JSON.stringify(event)));
const fiber = await root.plugin(danmakuApply, {
  roomId,
  uid: 0,
  buvid,
  token: server.token,
  connect: createWebSocketConnect(`wss://${host.host}:${host.wssPort}/sub`),
});

await new Promise((resolve) => setTimeout(resolve, seconds * 1000));
await fiber.dispose();
console.log('done');
process.exit(0);
