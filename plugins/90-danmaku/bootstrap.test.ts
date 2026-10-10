import { describe, expect, it, vi } from 'vitest';
import { resolveConnect } from './bootstrap.ts';

const NAV_BODY = {
  code: 0,
  data: {
    wbi_img: {
      img_url: 'https://i0.hdslb.com/bfs/wbi/7cd084941338484aae1ad9425b84077c.png',
      sub_url: 'https://i0.hdslb.com/bfs/wbi/4932caff0ff746eab6f01bf08b70ac45.png',
    },
  },
};

const DANMU_INFO_BODY = {
  code: 0,
  data: {
    token: 'TOKEN',
    host_list: [{ host: 'h1.chat.bilibili.com', port: 2243, wss_port: 443, ws_port: 2244 }],
  },
};

describe('resolveConnect', () => {
  it('自动取 buvid、解析弹幕服务器并拼出 wss connect', async () => {
    const fetch = vi.fn(async (url: string | URL | Request, _init?: RequestInit) => {
      if (String(url).includes('/web-interface/nav')) return new Response(JSON.stringify(NAV_BODY), { status: 200 });
      if (String(url).includes('getDanmuInfo')) return new Response(JSON.stringify(DANMU_INFO_BODY), { status: 200 });
      return new Response(null, {
        status: 200,
        headers: { 'set-cookie': 'buvid3=BUVID3VALUE; Path=/; Domain=.bilibili.com' },
      });
    });

    const resolved = await resolveConnect({ roomId: 22625027, fetch: fetch as unknown as typeof globalThis.fetch });

    expect(resolved.buvid).toBe('BUVID3VALUE');
    expect(resolved.token).toBe('TOKEN');
    const socket = resolved.connect({ onOpen() {}, onMessage() {}, onClose() {} });
    expect(typeof socket.send).toBe('function');
    expect(typeof socket.close).toBe('function');
  });

  it('传入 buvid 时不请求首页', async () => {
    const fetch = vi.fn(async (url: string | URL | Request, _init?: RequestInit) => {
      if (String(url).includes('/web-interface/nav')) return new Response(JSON.stringify(NAV_BODY), { status: 200 });
      return new Response(JSON.stringify(DANMU_INFO_BODY), { status: 200 });
    });

    const resolved = await resolveConnect({ roomId: 1, buvid: 'GIVEN', fetch: fetch as unknown as typeof globalThis.fetch });

    expect(resolved.buvid).toBe('GIVEN');
    // 只有 nav（WBI）+ getDanmuInfo 两次，没有首页取 buvid 的请求
    expect(fetch).toHaveBeenCalledTimes(2);
  });
});
