import { getMessages, getString, getVarint, readFields } from './protobuf.ts';

export type GiftEvent = {
  type: 'gift';
  uid: number;
  uname: string;
  giftName: string;
  num: number;
  price: number;
  coinType: string;
  timestamp: number;
};

export function decodeGiftV2(data: { pb: string }): GiftEvent[] {
  const broadcast = readFields(Buffer.from(data.pb, 'base64'));
  const uid = getVarint(broadcast, 1);
  const uname = getString(broadcast, 2);
  return getMessages(broadcast, 10).map((giftBytes) => {
    const gift = readFields(giftBytes);
    return {
      type: 'gift',
      uid,
      uname,
      giftName: getString(gift, 2),
      num: getVarint(gift, 3),
      price: getVarint(gift, 5),
      coinType: getString(gift, 8),
      timestamp: getVarint(gift, 10) * 1000,
    };
  });
}
