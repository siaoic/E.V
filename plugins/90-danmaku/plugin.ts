import type { Context } from '@deepseek-ai/cordis';
import type { GiftEvent } from './gift-v2.ts';
import type { DanmakuEvent, HeartbeatEvent, SuperChatEvent } from './normalize.ts';
import type { DanmakuSocket, DanmakuSocketHandlers } from './source.ts';
import { BiliDanmakuSource } from './source.ts';
import { resolveConnect } from './bootstrap.ts';

export type LiveEvent = DanmakuEvent | SuperChatEvent | GiftEvent | HeartbeatEvent;

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
  token?: string;
  heartbeatMs?: number | string;
  connect?: DanmakuConnect;
};

export const apply = Object.assign(
  async function danmaku(ctx: Context, config: DanmakuPluginConfig) {
    const roomId = Number(config.roomId);
    if (!Number.isFinite(roomId)) throw new Error(`roomId 无效：${String(config.roomId)}`);
    const uid = config.uid === undefined ? 0 : Number(config.uid);
    const heartbeatMs = config.heartbeatMs === undefined ? 2000 : Number(config.heartbeatMs);
    const resolved = config.connect
      ? { connect: config.connect, buvid: config.buvid ?? '', token: config.token ?? '' }
      : await resolveConnect({ roomId, buvid: config.buvid });
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
  heartbeatMs: '${HEARTBEAT_MS:-2000}',
};
