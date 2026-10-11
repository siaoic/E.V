import { mixinKey, signWbi } from './wbi.ts';

type FetchLike = typeof globalThis.fetch;

export type DanmuServer = {
  token: string;
  hosts: { host: string; wssPort: number }[];
  /** SESSDATA 有效时 nav 返回的登录 uid；匿名连接为 undefined */
  uid?: number;
};

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

// 带 SESSDATA 时把登录 Cookie 一起送上去（buvid3 保留，风控要认它）
function requestHeaders(buvid: string, sessdata?: string): Record<string, string> {
  const cookies = [`buvid3=${buvid}`];
  if (sessdata) cookies.push(`SESSDATA=${sessdata}`);
  return { 'User-Agent': USER_AGENT, Cookie: cookies.join('; ') };
}

// nav 一次给两样东西：WBI 签名密钥，以及登录态下的 uid
async function fetchNavInfo(fetch: FetchLike, buvid: string, sessdata?: string): Promise<{ key: string; uid?: number }> {
  const response = await fetch(NAV_URL, { headers: requestHeaders(buvid, sessdata) });
  const body = (await response.json()) as {
    data?: { mid?: number; wbi_img?: { img_url: string; sub_url: string } };
  };
  const info = body.data;
  if (info?.wbi_img === undefined) throw new Error('nav 未返回 wbi_img：Cookie 失效或触发风控');
  const fileKey = (url: string) => url.slice(url.lastIndexOf('/') + 1).split('.')[0];
  return {
    key: mixinKey(fileKey(info.wbi_img.img_url), fileKey(info.wbi_img.sub_url)),
    uid: typeof info.mid === 'number' && info.mid > 0 ? info.mid : undefined,
  };
}

export async function fetchDanmuServer({
  roomId,
  buvid,
  sessdata,
  fetch = globalThis.fetch,
  now = Date.now(),
}: {
  roomId: number;
  buvid: string;
  sessdata?: string;
  fetch?: FetchLike;
  now?: number;
}): Promise<DanmuServer> {
  const { key, uid } = await fetchNavInfo(fetch, buvid, sessdata);
  const params = signWbi({ id: roomId, type: 0 }, key, String(Math.floor(now / 1000)));
  const query = new URLSearchParams(Object.entries(params).map(([k, v]) => [k, String(v)]));
  const response = await fetch(`${DANMU_INFO_URL}?${query}`, { headers: requestHeaders(buvid, sessdata) });
  const data = (await response.json()) as {
    code: number;
    message?: string;
    data: { token: string; host_list: { host: string; wss_port: number }[] };
  };
  if (data.code !== 0) throw new Error(`getDanmuInfo failed: ${data.message ?? data.code}`);
  return {
    token: data.data.token,
    hosts: data.data.host_list.map((h) => ({ host: h.host, wssPort: h.wss_port })),
    uid,
  };
}
