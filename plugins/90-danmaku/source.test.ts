import { afterEach, beforeEach, describe, expect, it, type Mock, vi } from 'vitest';
import { buildAuthPacket } from './auth.ts';
import { encodePacket, Operation } from './codec.ts';
import type { DanmakuEvent, SuperChatEvent } from './normalize.ts';
import { BiliDanmakuSource, type DanmakuSocketHandlers } from './source.ts';
import type { GiftEvent } from './gift-v2.ts';

function frame(operation: number, ver: number, body: Buffer): Buffer {
  const header = Buffer.alloc(16);
  header.writeUInt32BE(16 + body.length, 0);
  header.writeUInt16BE(16, 4);
  header.writeUInt16BE(ver, 6);
  header.writeUInt32BE(operation, 8);
  header.writeUInt32BE(1, 12);
  return Buffer.concat([header, body]);
}

function varint(value: number): number[] {
  const out: number[] = [];
  let n = value;
  while (n >= 0x80) {
    out.push((n % 0x80) | 0x80);
    n = Math.floor(n / 0x80);
  }
  out.push(n);
  return out;
}

const pbNum = (field: number, value: number) => Buffer.from([field << 3, ...varint(value)]);
const pbStr = (field: number, value: string) => {
  const bytes = Buffer.from(value, 'utf8');
  return Buffer.from([(field << 3) | 2, ...varint(bytes.length), ...bytes]);
};
const pbMsg = (field: number, body: Buffer) =>
  Buffer.concat([Buffer.from([(field << 3) | 2, ...varint(body.length)]), body]);

const giftPb = Buffer.concat([
  pbNum(1, 123456),
  pbStr(2, '测试观众'),
  pbMsg(
    10,
    Buffer.concat([
      pbNum(1, 31164),
      pbStr(2, '测试礼物'),
      pbNum(3, 2),
      pbNum(5, 100),
      pbStr(8, 'gold'),
      pbNum(10, 1791548900),
    ]),
  ),
]);

const giftFrame = frame(
  Operation.SEND_MSG_REPLY,
  0,
  Buffer.from(JSON.stringify({ cmd: 'SEND_GIFT_V2', data: { pb: giftPb.toString('base64') } })),
);

const danmakuFrame = frame(
  Operation.SEND_MSG_REPLY,
  0,
  Buffer.from(
    JSON.stringify({
      cmd: 'DANMU_MSG',
      info: [
        [0, 1, 25, 16777215, 1791548872188, 1791547601, 0, '746db7bb', 0, 0, 0, '', 0, '{}', '{}', {}],
        '小狗',
        [123456, '测试观众', 0, 0, 0, 0, 0, ''],
      ],
    }),
  ),
);

const entryEffectFrame = frame(Operation.SEND_MSG_REPLY, 0, Buffer.from('{"cmd":"ENTRY_EFFECT"}'));

describe('BiliDanmakuSource', () => {
  let handlers: DanmakuSocketHandlers;
  let socket: { send: Mock<(packet: Buffer) => void>; close: Mock<() => void> };
  let onEvent: Mock<(event: DanmakuEvent | SuperChatEvent | GiftEvent) => void>;
  let source: BiliDanmakuSource;

  beforeEach(() => {
    vi.useFakeTimers();
    socket = { send: vi.fn<(packet: Buffer) => void>(), close: vi.fn<() => void>() };
    onEvent = vi.fn<(event: DanmakuEvent | SuperChatEvent | GiftEvent) => void>();
    source = new BiliDanmakuSource({
      roomId: 22625027,
      uid: 0,
      buvid: 'BUVID',
      token: 'TOKEN',
      heartbeatMs: 30_000,
      connect: (h) => {
        handlers = h;
        return socket;
      },
      onEvent,
    });
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('连接建立后先发送认证包', () => {
    source.start();
    handlers.onOpen();

    expect(socket.send).toHaveBeenCalledTimes(1);
    expect(socket.send.mock.calls[0][0]).toEqual(
      buildAuthPacket({ roomId: 22625027, uid: 0, buvid: 'BUVID', token: 'TOKEN' }),
    );
  });

  it('认证后按间隔发送心跳包', () => {
    source.start();
    handlers.onOpen();
    socket.send.mockClear();

    vi.advanceTimersByTime(30_000);

    expect(socket.send).toHaveBeenCalledTimes(1);
    expect(socket.send.mock.calls[0][0]).toEqual(encodePacket(Operation.HEARTBEAT, {}));
  });

  it('收到弹幕帧时把归一化后的事件交给 onEvent', () => {
    source.start();
    handlers.onMessage(danmakuFrame);

    expect(onEvent).toHaveBeenCalledWith({
      type: 'danmaku',
      uid: 123456,
      uname: '测试观众',
      text: '小狗',
      timestamp: 1791548872188,
    });
  });

  it('收到礼物帧时把礼物事件交给 onEvent', () => {
    source.start();
    handlers.onMessage(giftFrame);

    expect(onEvent).toHaveBeenCalledWith({
      type: 'gift',
      uid: 123456,
      uname: '测试观众',
      giftName: '测试礼物',
      num: 2,
      price: 100,
      coinType: 'gold',
      timestamp: 1791548900000,
    });
  });

  it('无关命令不产生事件', () => {
    source.start();
    handlers.onMessage(entryEffectFrame);

    expect(onEvent).not.toHaveBeenCalled();
  });

  it('stop 后停止心跳并关闭连接', () => {
    source.start();
    handlers.onOpen();
    source.stop();
    socket.send.mockClear();

    vi.advanceTimersByTime(60_000);

    expect(socket.send).not.toHaveBeenCalled();
    expect(socket.close).toHaveBeenCalledTimes(1);
  });
});

describe('BiliDanmakuSource 断线重连', () => {
  type Attempt = { handlers: DanmakuSocketHandlers; send: Mock<(packet: Buffer) => void> };

  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  const makeSource = (onEvent: Mock<(event: never) => void>) => {
    const attempts: Attempt[] = [];
    const source = new BiliDanmakuSource({
      roomId: 22625027,
      uid: 0,
      buvid: 'BUVID',
      token: 'TOKEN',
      connect: (h) => {
        const socket = { send: vi.fn<(packet: Buffer) => void>(), close: vi.fn<() => void>() };
        attempts.push({ handlers: h, send: socket.send });
        return socket;
      },
      onEvent: onEvent as never,
    });
    source.start();
    return { source, attempts };
  };

  it('断线后按退避自动重连并重新认证', () => {
    const { attempts } = makeSource(vi.fn());

    attempts[0]!.handlers.onClose();
    vi.advanceTimersByTime(999);
    expect(attempts).toHaveLength(1);
    vi.advanceTimersByTime(1);
    expect(attempts).toHaveLength(2);

    attempts[1]!.handlers.onOpen();
    expect(attempts[1]!.send).toHaveBeenCalledWith(
      buildAuthPacket({ roomId: 22625027, uid: 0, buvid: 'BUVID', token: 'TOKEN' }),
    );
  });

  it('连续断线退避翻倍，认证成功后重置', () => {
    const { attempts } = makeSource(vi.fn());

    attempts[0]!.handlers.onClose();
    vi.advanceTimersByTime(1000);
    expect(attempts).toHaveLength(2);

    attempts[1]!.handlers.onClose();
    vi.advanceTimersByTime(1999);
    expect(attempts).toHaveLength(2);
    vi.advanceTimersByTime(1);
    expect(attempts).toHaveLength(3);

    attempts[2]!.handlers.onOpen();
    attempts[2]!.handlers.onClose();
    vi.advanceTimersByTime(999);
    expect(attempts).toHaveLength(3);
    vi.advanceTimersByTime(1);
    expect(attempts).toHaveLength(4);
  });

  it('stop 后不再重连', () => {
    const { source, attempts } = makeSource(vi.fn());

    attempts[0]!.handlers.onClose();
    source.stop();
    vi.advanceTimersByTime(60_000);

    expect(attempts).toHaveLength(1);
  });

  it('旧连接迟到的关闭事件不影响新连接', () => {
    const { attempts } = makeSource(vi.fn());

    attempts[0]!.handlers.onClose();
    vi.advanceTimersByTime(1000);
    attempts[1]!.handlers.onOpen();
    attempts[0]!.handlers.onClose();
    vi.advanceTimersByTime(60_000);

    expect(attempts).toHaveLength(2);
  });
});
