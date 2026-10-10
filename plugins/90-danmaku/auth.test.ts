import { describe, expect, it } from 'vitest';
import { buildAuthPacket } from './auth.ts';
import { Operation } from './codec.ts';

describe('buildAuthPacket', () => {
  it('认证包使用 AUTH 操作码，包体含房间、用户、buvid 与 token', () => {
    const packet = buildAuthPacket({ roomId: 22625027, uid: 0, buvid: 'BUVID', token: 'TOKEN' });

    expect(packet.readUInt32BE(8)).toBe(Operation.AUTH);
    expect(JSON.parse(packet.subarray(16).toString('utf8'))).toEqual({
      uid: 0,
      roomid: 22625027,
      protover: 3,
      platform: 'web',
      type: 2,
      buvid: 'BUVID',
      key: 'TOKEN',
    });
  });

  it('没有 token 时包体不含 key 字段', () => {
    const packet = buildAuthPacket({ roomId: 22625027, uid: 0, buvid: 'BUVID' });

    expect(JSON.parse(packet.subarray(16).toString('utf8'))).not.toHaveProperty('key');
  });
});
