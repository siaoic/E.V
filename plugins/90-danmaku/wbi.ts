import { createHash } from 'node:crypto';

const MIXIN_KEY_INDEX_TABLE = [
  46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38,
  41, 13,
];

export function mixinKey(imgKey: string, subKey: string): string {
  const shuffled = imgKey + subKey;
  return MIXIN_KEY_INDEX_TABLE.filter((index) => index < shuffled.length)
    .map((index) => shuffled[index])
    .join('');
}

export function signWbi(
  params: Record<string, string | number>,
  key: string,
  wts: string,
): Record<string, string | number> {
  const toSign: Record<string, string> = {};
  for (const name of Object.keys({ ...params, wts }).sort()) {
    const raw = name === 'wts' ? wts : String(params[name]);
    toSign[name] = raw.replace(/[!'()*]/g, '');
  }
  const query = Object.entries(toSign)
    .map(([name, value]) => `${encodeURIComponent(name)}=${encodeURIComponent(value).replace(/%20/g, '+')}`)
    .join('&');
  const wRid = createHash('md5')
    .update(query + key)
    .digest('hex');
  return { ...params, wts, w_rid: wRid };
}
