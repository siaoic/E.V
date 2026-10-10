import { describe, expect, it } from 'vitest';
import { mixinKey, signWbi } from './wbi.ts';

const IMG_KEY = '7cd084941338484aae1ad9425b84077c';
const SUB_KEY = '4932caff0ff746eab6f01bf08b70ac45';

describe('mixinKey', () => {
  it('按置换表从 img_key + sub_key 取出 32 位口令', () => {
    expect(mixinKey(IMG_KEY, SUB_KEY)).toBe('ea1db124af3c7062474693fa704f4ff8');
  });
});

describe('signWbi', () => {
  it('按 key 排序、过滤 !\'()* 后签名，附加 wts 与 w_rid', () => {
    const signed = signWbi({ room_id: 22625027, type: 0, name: 'a b!(c)' }, mixinKey(IMG_KEY, SUB_KEY), '1791548900');

    expect(signed).toEqual({
      room_id: 22625027,
      type: 0,
      name: 'a b!(c)',
      wts: '1791548900',
      w_rid: 'f8f5e412ff6ebf4041740f24a759f458',
    });
  });
});
