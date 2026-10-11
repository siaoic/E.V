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

  it('sessdata 透传到 B站请求，并把登录 uid 带出来', async () => {
    const fetch = vi.fn(async (url: string | URL | Request, _init?: RequestInit) => {
      if (String(url).includes('/web-interface/nav')) {
        return new Response(
          JSON.stringify({ code: 0, data: { mid: 123456, wbi_img: NAV_BODY.data.wbi_img } }),
          { status: 200 },
        );
      }
      return new Response(JSON.stringify(DANMU_INFO_BODY), { status: 200 });
    });

    const resolved = await resolveConnect({
      roomId: 22748536,
      buvid: 'GIVEN',
      sessdata: 'SESSDATA-VALUE',
      fetch: fetch as unknown as typeof globalThis.fetch,
    });

    expect(resolved.uid).toBe(123456);
    for (const call of fetch.mock.calls) {
      expect((call[1]?.headers as Record<string, string>).Cookie).toBe('buvid3=GIVEN; SESSDATA=SESSDATA-VALUE');
    }
  });

  it('不传 sessdata 时保持匿名（无 uid、Cookie 只有 buvid3）', async () => {
    const fetch = vi.fn(async (url: string | URL | Request, _init?: RequestInit) => {
      if (String(url).includes('/web-interface/nav')) return new Response(JSON.stringify(NAV_BODY), { status: 200 });
      return new Response(JSON.stringify(DANMU_INFO_BODY), { status: 200 });
    });

    const resolved = await resolveConnect({
      roomId: 22748536,
      buvid: 'GIVEN',
      fetch: fetch as unknown as typeof globalThis.fetch,
    });

    expect(resolved.uid).toBeUndefined();
    expect((fetch.mock.calls[0][1]?.headers as Record<string, string>).Cookie).toBe('buvid3=GIVEN');
  });
});
