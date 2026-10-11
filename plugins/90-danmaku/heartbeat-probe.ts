// 只测心跳节奏的探针：连上直播间，记录 HEARTBEAT_REPLY 的到达间隔，跑满 N 秒后打印分布。
// 为什么需要它：零计时器架构把「心跳拍数」当拍子用（dream 每 30 拍整理一轮、429 降温 10 拍），
// 但一拍到底多少秒没有实测过——文档里「30 拍 ≈ 半小时」与插件里 HEARTBEAT_MS=2000 互相矛盾。
// 匿名连接即可（uid 0 + buvid3），不需要任何凭据。
//
//   node plugins/90-danmaku/heartbeat-probe.ts [roomId] [seconds]
import { Context } from '@deepseek-ai/cordis';
import { apply as danmakuApply, type LiveEvent } from './plugin.ts';
import { fetchBuvid3, fetchDanmuServer } from './server.ts';
import { createWebSocketConnect } from './transport.ts';

const roomId = Number(process.argv[2] ?? 22748536);
const seconds = Number(process.argv[3] ?? 120);

const buvid = await fetchBuvid3();
const server = await fetchDanmuServer({ roomId, buvid });
const host = server.hosts[0];
console.log(`room=${roomId} host=${host.host}:${host.wssPort} 观测 ${seconds}s（HEARTBEAT_MS=${process.env.HEARTBEAT_MS ?? '2000 默认'}）`);

const beats: number[] = [];
let danmaku = 0;
let others = 0;
const root = new Context();
root.on('world/live-event', (event: LiveEvent) => {
  if (event.type === 'heartbeat') {
    beats.push(Date.now());
    console.log(`[beat ${String(beats.length).padStart(3)}] count=${event.count ?? '未知'}`);
    return;
  }
  if (event.type === 'danmaku') danmaku += 1;
  else others += 1;
});

const fiber = await root.plugin(danmakuApply, {
  roomId,
  uid: 0,
  buvid,
  token: server.token,
  connect: createWebSocketConnect(`wss://${host.host}:${host.wssPort}/sub`),
});

const started = Date.now();
await new Promise((resolve) => setTimeout(resolve, seconds * 1000));
const observed = (Date.now() - started) / 1000;
await fiber.dispose();

const gaps = beats.slice(1).map((at, i) => (at - beats[i]!) / 1000);
const round = (n: number) => Math.round(n * 100) / 100;
const median = (values: number[]): number => {
  if (values.length === 0) return 0;
  const sorted = [...values].sort((a, b) => a - b);
  const mid = Math.floor(sorted.length / 2);
  return sorted.length % 2 === 1 ? sorted[mid]! : (sorted[mid - 1]! + sorted[mid]!) / 2;
};
const histogram = new Map<number, number>();
for (const gap of gaps) {
  const bucket = Math.round(gap * 2) / 2;
  histogram.set(bucket, (histogram.get(bucket) ?? 0) + 1);
}
const spread = [...histogram.entries()].sort((a, b) => b[1] - a[1]).slice(0, 5);

console.log('--- 结果 ---');
console.log(`观测 ${round(observed)}s：心跳 ${beats.length} 次，弹幕 ${danmaku} 条，其他事件 ${others} 条`);
if (gaps.length > 0) {
  const mean = gaps.reduce((sum, gap) => sum + gap, 0) / gaps.length;
  console.log(
    `间隔 平均 ${round(mean)}s / 中位 ${round(median(gaps))}s / 最小 ${round(Math.min(...gaps))}s / 最大 ${round(Math.max(...gaps))}s`,
  );
  console.log(`分布 ${spread.map(([bucket, n]) => `${bucket}s×${n}`).join('、')}`);
  console.log(`推算：30 拍 ≈ ${round((mean * 30) / 60)} 分钟；900 拍 ≈ ${round((mean * 900) / 60)} 分钟`);
} else {
  console.log('没有收到任何心跳：检查房间号或网络；此房间可能不推 HEARTBEAT_REPLY');
}
console.log(
  `JSON ${JSON.stringify({
    roomId,
    observedSeconds: round(observed),
    heartbeats: beats.length,
    danmaku,
    others,
    meanGap: gaps.length > 0 ? round(gaps.reduce((s, g) => s + g, 0) / gaps.length) : null,
    medianGap: gaps.length > 0 ? round(median(gaps)) : null,
  })}`,
);
process.exit(0);
