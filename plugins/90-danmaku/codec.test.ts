import { brotliCompressSync } from 'node:zlib';
import { describe, expect, it } from 'vitest';
import { decodeCommands, encodePacket, Operation } from './codec.ts';

function frame(operation: number, ver: number, body: Buffer): Buffer {
  const header = Buffer.alloc(16);
  header.writeUInt32BE(16 + body.length, 0);
  header.writeUInt16BE(16, 4);
  header.writeUInt16BE(ver, 6);
  header.writeUInt32BE(operation, 8);
  header.writeUInt32BE(1, 12);
  return Buffer.concat([header, body]);
}

describe('encodePacket', () => {
  it('心跳包 {} 编码为 16 字节头加 JSON 包体', () => {
    const packet = encodePacket(Operation.HEARTBEAT, {});

    expect(packet.toString('hex')).toBe('00000012001000010000000200000001' + '7b7d');
  });
});

describe('decodeCommands', () => {
  it('解开 brotli 压缩的业务包，并按包长拆出其中的多条消息', () => {
    const inner = Buffer.concat([
      frame(Operation.SEND_MSG_REPLY, 0, Buffer.from('{"cmd":"DANMU_MSG"}')),
      frame(Operation.SEND_MSG_REPLY, 0, Buffer.from('{"cmd":"ENTRY_EFFECT"}')),
    ]);
    const outer = frame(Operation.SEND_MSG_REPLY, 3, brotliCompressSync(inner));

    expect(decodeCommands(outer)).toEqual([{ cmd: 'DANMU_MSG' }, { cmd: 'ENTRY_EFFECT' }]);
  });

  it('心跳回复解码为带人气值的命令，包体为人气值文本', () => {
    const packet = frame(Operation.HEARTBEAT_REPLY, 0, Buffer.from('48203'));

    expect(decodeCommands(packet)).toEqual([{ cmd: 'HEARTBEAT_REPLY', count: 48203 }]);
  });

  it('空包体的心跳回复解码为人气值未知的命令', () => {
    const packet = frame(Operation.HEARTBEAT_REPLY, 0, Buffer.alloc(0));

    expect(decodeCommands(packet)).toEqual([{ cmd: 'HEARTBEAT_REPLY', count: null }]);
  });
});
