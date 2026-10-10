import { Context } from '@deepseek-ai/cordis';
import { describe, expect, it, type Mock, vi } from 'vitest';
import { Operation } from './codec.ts';
import { apply } from './plugin.ts';
import type { DanmakuSocketHandlers } from './source.ts';

function frame(operation: number, ver: number, body: Buffer): Buffer {
  const header = Buffer.alloc(16);
  header.writeUInt32BE(16 + body.length, 0);
  header.writeUInt16BE(16, 4);
  header.writeUInt16BE(ver, 6);
  header.writeUInt32BE(operation, 8);
  header.writeUInt32BE(1, 12);
  return Buffer.concat([header, body]);
}

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

describe('danmaku plugin', () => {
  it('启动后把弹幕事件发到总线，卸载时关闭连接', async () => {
    let handlers: DanmakuSocketHandlers | undefined;
    const socket = {
      send: vi.fn<(packet: Buffer) => void>(),
      close: vi.fn<() => void>(),
    };
    const root = new Context();
    const listener = vi.fn();
    root.on('world/live-event', listener);

    const fiber = await root.plugin(apply, {
      roomId: 22625027,
      uid: 0,
      buvid: 'BUVID',
      connect: (h) => {
        handlers = h;
        return socket;
      },
    });

    handlers?.onMessage(danmakuFrame);
    expect(listener).toHaveBeenCalledWith({
      type: 'danmaku',
      uid: 123456,
      uname: '测试观众',
      text: '小狗',
      timestamp: 1791548872188,
    });

    await fiber.dispose();
    expect(socket.close).toHaveBeenCalledTimes(1);
  });
});
