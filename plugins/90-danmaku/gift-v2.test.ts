import { describe, expect, it } from 'vitest';
import { decodeGiftV2 } from './gift-v2.ts';

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

const num = (field: number, value: number) => Buffer.from([field << 3, ...varint(value)]);
const str = (field: number, value: string) => {
  const bytes = Buffer.from(value, 'utf8');
  return Buffer.from([(field << 3) | 2, ...varint(bytes.length), ...bytes]);
};
const msg = (field: number, body: Buffer) =>
  Buffer.concat([Buffer.from([(field << 3) | 2, ...varint(body.length)]), body]);

describe('decodeGiftV2', () => {
  it('从 protobuf 的礼物列表中逐条取出礼物，并跳过未使用的字段', () => {
    const giftA = Buffer.concat([
      num(1, 31164),
      str(2, '测试礼物'),
      num(3, 2),
      num(5, 100),
      str(8, 'gold'),
      num(10, 1791548900),
    ]);
    const giftB = Buffer.concat([
      num(1, 31165),
      str(2, '另一礼物'),
      num(3, 1),
      num(5, 1000),
      str(8, 'silver'),
      num(10, 1791548929),
    ]);
    const pb = Buffer.concat([
      num(1, 123456),
      str(2, '测试观众'),
      num(5, 0),
      msg(10, giftA),
      msg(10, giftB),
    ]);

    expect(decodeGiftV2({ pb: pb.toString('base64') })).toEqual([
      {
        type: 'gift',
        uid: 123456,
        uname: '测试观众',
        giftName: '测试礼物',
        num: 2,
        price: 100,
        coinType: 'gold',
        timestamp: 1791548900000,
      },
      {
        type: 'gift',
        uid: 123456,
        uname: '测试观众',
        giftName: '另一礼物',
        num: 1,
        price: 1000,
        coinType: 'silver',
        timestamp: 1791548929000,
      },
    ]);
  });
});
