import { mixinKey, signWbi } from './wbi.ts';

type FetchLike = typeof globalThis.fetch;

export type DanmuServer = { token: string; hosts: { host: string; wssPort: number }[] };

const NAV_URL = 'https://api.bilibili.com/x/web-interface/nav';
const HOME_URL = 'https://www.bilibili.com/';
const DANMU_INFO_URL = 'https://api.live.bilibili.com/xlive/web-room/v1/index/getDanmuInfo';
const USER_AGENT =
  'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36';

export async function fetchBuvid3(fetch: FetchLike = globalThis.fetch): Promise<string> {
  const response = await fetch(HOME_URL, { headers: { 'User-Agent': USER_AGENT } });
  const cookie = response.headers.getSetCookie().find((c) => c.startsWith('buvid3='));
  if (cookie === undefined) throw new Error('buvid3 cookie missing from bilibili home page');
  return cookie.slice('buvid3='.length).split(';')[0];
}

async function fetchWbiKey(fetch: FetchLike, buvid: string): Promise<string> {
  const data = (await (await fetch(NAV_URL, { headers: requestHeaders(buvid) })).json()) as {
    data: { wbi_img: { img_url: string; sub_url: string } };
  };
  const fileKey = (url: string) => url.slice(url.lastIndexOf('/') + 1).split('.')[0];
  return mixinKey(fileKey(data.data.wbi_img.img_url), fileKey(data.data.wbi_img.sub_url));
}

function requestHeaders(buvid: string): Record<string, string> {
  return { 'User-Agent': USER_AGENT, Cookie: `buvid3=${buvid}` };
}

export async function fetchDanmuServer({
  roomId,
  buvid,
  fetch = globalThis.fetch,
  now = Date.now(),
}: {
  roomId: number;
  buvid: string;
  fetch?: FetchLike;
  now?: number;
}): Promise<DanmuServer> {
  const key = await fetchWbiKey(fetch, buvid);
  const params = signWbi({ id: roomId, type: 0 }, key, String(Math.floor(now / 1000)));
  const query = new URLSearchParams(Object.entries(params).map(([k, v]) => [k, String(v)]));
  const response = await fetch(`${DANMU_INFO_URL}?${query}`, { headers: requestHeaders(buvid) });
  const data = (await response.json()) as {
    code: number;
    message?: string;
    data: { token: string; host_list: { host: string; wss_port: number }[] };
  };
  if (data.code !== 0) throw new Error(`getDanmuInfo failed: ${data.message ?? data.code}`);
  return {
    token: data.data.token,
    hosts: data.data.host_list.map((h) => ({ host: h.host, wssPort: h.wss_port })),
  };
}
