import type { Context } from '@deepseek-ai/cordis';
import type { GiftEvent } from './gift-v2.ts';
import type { DanmakuEvent, GuardEvent, HeartbeatEvent, SuperChatEvent } from './normalize.ts';
import type { DanmakuSocket, DanmakuSocketHandlers } from './source.ts';
import { BiliDanmakuSource } from './source.ts';
import { resolveConnect } from './bootstrap.ts';

export type LiveEvent = DanmakuEvent | SuperChatEvent | GiftEvent | GuardEvent | HeartbeatEvent;

export type DanmakuConnect = (handlers: DanmakuSocketHandlers) => DanmakuSocket;

declare module '@deepseek-ai/cordis' {
  interface Events {
    'world/live-event'(event: LiveEvent): void;
  }
}

// 纯 JSON 可序列化；connect 保留为测试注入口，生产走 bootstrap.resolveConnect
export type DanmakuPluginConfig = {
  roomId: number | string;
  uid?: number | string;
  buvid?: string;
  /** B 站登录 Cookie；配了就带登录态取 uid，不配则匿名（uid=0） */
  sessdata?: string;
  token?: string;
  heartbeatMs?: number | string;
  connect?: DanmakuConnect;
};

export const apply = Object.assign(
  async function danmaku(ctx: Context, config: DanmakuPluginConfig) {
    const roomId = Number(config.roomId);
    if (!Number.isFinite(roomId)) throw new Error(`roomId 无效：${String(config.roomId)}`);
    const configuredUid = config.uid === undefined ? 0 : Number(config.uid);
    const heartbeatMs = config.heartbeatMs === undefined ? 2000 : Number(config.heartbeatMs);
    const sessdata = config.sessdata?.trim() || undefined;
    const resolved = config.connect
      ? { connect: config.connect, buvid: config.buvid ?? '', token: config.token ?? '', uid: undefined }
      : await resolveConnect({ roomId, buvid: config.buvid, sessdata });
    // uid 显式配了正数就用它；否则用登录态返回的 uid；都没有则匿名 0
    const uid = configuredUid > 0 ? configuredUid : (resolved.uid ?? configuredUid);
    if (sessdata) console.log(`[弹幕] 带 SESSDATA 登录态（uid=${uid}）`);
    const source = new BiliDanmakuSource({
      roomId,
      uid,
      buvid: resolved.buvid,
      token: config.token ?? resolved.token,
      heartbeatMs,
      connect: resolved.connect,
      onEvent: (event) => ctx.emit('world/live-event', event),
    });
    ctx.effect(() => {
      source.start();
      return () => source.stop();
    });
  },
  { inject: [] as string[] },
);

export const config = {
  roomId: '${ROOM_ID:-22748536}',
  uid: 0,
  sessdata: '${SESSDATA:-}',
  heartbeatMs: '${HEARTBEAT_MS:-2000}',
};
