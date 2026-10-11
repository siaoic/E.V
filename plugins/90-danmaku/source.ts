import { buildAuthPacket } from './auth.ts';
import { decodeCommands, encodePacket, Operation } from './codec.ts';
import { decodeGiftV2, type GiftEvent } from './gift-v2.ts';
import { type DanmakuEvent, type GuardEvent, type HeartbeatEvent, normalizeCommand, type SuperChatEvent } from './normalize.ts';

export type DanmakuSocket = { send(packet: Buffer): void; close(): void };

export type DanmakuSocketHandlers = { onOpen(): void; onMessage(data: Buffer): void; onClose(): void };

export type BiliDanmakuSourceOptions = {
  roomId: number;
  uid: number;
  buvid: string;
  token?: string;
  heartbeatMs?: number;
  connect(handlers: DanmakuSocketHandlers): DanmakuSocket;
  onEvent(event: DanmakuEvent | SuperChatEvent | GiftEvent | GuardEvent | HeartbeatEvent): void;
};

const RECONNECT_BASE_MS = 1000;
const RECONNECT_MAX_MS = 30_000;

export class BiliDanmakuSource {
  private readonly options: BiliDanmakuSourceOptions;
  private socket: DanmakuSocket | undefined;
  private heartbeat: ReturnType<typeof setInterval> | undefined;
  private reconnectTimer: ReturnType<typeof setTimeout> | undefined;
  private reconnectDelay = RECONNECT_BASE_MS;
  private stopped = false;

  constructor(options: BiliDanmakuSourceOptions) {
    this.options = options;
  }

  start(): void {
    this.stopped = false;
    this.reconnectDelay = RECONNECT_BASE_MS;
    this.connect();
  }

  stop(): void {
    this.stopped = true;
    if (this.reconnectTimer !== undefined) clearTimeout(this.reconnectTimer);
    this.reconnectTimer = undefined;
    if (this.heartbeat !== undefined) clearInterval(this.heartbeat);
    this.heartbeat = undefined;
    this.socket?.close();
    this.socket = undefined;
  }

  private connect(): void {
    let socket: DanmakuSocket | undefined;
    socket = this.options.connect({
      onOpen: () => this.handleOpen(socket),
      onMessage: (data) => this.handleMessage(data),
      onClose: () => this.handleClose(socket),
    });
    this.socket = socket;
  }

  private handleOpen(socket: DanmakuSocket | undefined): void {
    if (socket === undefined || socket !== this.socket) return;
    const { roomId, uid, buvid, token, heartbeatMs = 30_000 } = this.options;
    this.reconnectDelay = RECONNECT_BASE_MS;
    if (this.heartbeat !== undefined) clearInterval(this.heartbeat);
    socket.send(buildAuthPacket({ roomId, uid, buvid, token }));
    this.heartbeat = setInterval(() => {
      this.socket?.send(encodePacket(Operation.HEARTBEAT, {}));
    }, heartbeatMs);
  }

  private handleClose(socket: DanmakuSocket | undefined): void {
    if (socket === undefined || socket !== this.socket) return;
    if (this.heartbeat !== undefined) clearInterval(this.heartbeat);
    this.heartbeat = undefined;
    if (this.stopped) return;
    const delay = this.reconnectDelay;
    this.reconnectDelay = Math.min(this.reconnectDelay * 2, RECONNECT_MAX_MS);
    this.reconnectTimer = setTimeout(() => this.connect(), delay);
  }

  private handleMessage(data: Buffer): void {
    for (const command of decodeCommands(data)) {
      if (command.cmd === 'SEND_GIFT_V2') {
        for (const gift of decodeGiftV2(command.data as { pb: string })) this.options.onEvent(gift);
        continue;
      }
      const event = normalizeCommand(command);
      if (event !== null) this.options.onEvent(event);
    }
  }
}
