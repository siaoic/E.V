import { describe, expect, it, vi } from 'vitest';
import { fetchBuvid3, fetchDanmuServer } from './server.ts';

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

const NAV_BODY_LOGGED_IN = {
  code: 0,
  data: {
    mid: 123456,
    wbi_img: {
      img_url: 'https://i0.hdslb.com/bfs/wbi/7cd084941338484aae1ad9425b84077c.png',
      sub_url: 'https://i0.hdslb.com/bfs/wbi/4932caff0ff746eab6f01bf08b70ac45.png',
    },
  },
};

describe('fetchBuvid3', () => {
  it('从首页响应的 Set-Cookie 中取出 buvid3', async () => {
    const fetch = vi.fn(
      async (_url: string | URL | Request, _init?: RequestInit) =>
        new Response(null, {
          status: 200,
          headers: { 'set-cookie': 'buvid3=BUVID3VALUE; Path=/; Domain=.bilibili.com' },
        }),
    );

    expect(await fetchBuvid3(fetch)).toBe('BUVID3VALUE');
    const headers = fetch.mock.calls[0][1]?.headers as Record<string, string>;
    expect(headers['User-Agent']).toContain('Chrome');
  });
});

describe('fetchDanmuServer', () => {
  it('用 WBI 签名请求弹幕服务器配置，返回 token 与 wss 主机', async () => {
    const fetch = vi.fn(async (url: string | URL | Request, _init?: RequestInit) => {
      const body = String(url).includes('/web-interface/nav') ? NAV_BODY : DANMU_INFO_BODY;
      return new Response(JSON.stringify(body), { status: 200 });
    });

    const server = await fetchDanmuServer({ roomId: 22625027, buvid: 'BUVID3VALUE', fetch, now: 1791548900000 });

    expect(server).toEqual({ token: 'TOKEN', hosts: [{ host: 'h1.chat.bilibili.com', wssPort: 443 }] });
    const danmuCall = fetch.mock.calls.find((call) => String(call[0]).includes('getDanmuInfo'));
    expect(String(danmuCall?.[0])).toBe(
      'https://api.live.bilibili.com/xlive/web-room/v1/index/getDanmuInfo?id=22625027&type=0&wts=1791548900&w_rid=3e58c26c7d076fc3c7a443dabaee0aab',
    );
    const headers = danmuCall?.[1]?.headers as Record<string, string>;
    expect(headers.Cookie).toBe('buvid3=BUVID3VALUE');
    expect(headers['User-Agent']).toContain('Chrome');
    // 匿名连接没有登录 uid
    expect(server.uid).toBeUndefined();
  });

  it('带 sessdata 时 nav 与 getDanmuInfo 都送登录 Cookie，并带回登录 uid', async () => {
    const fetch = vi.fn(async (url: string | URL | Request, _init?: RequestInit) => {
      const body = String(url).includes('/web-interface/nav') ? NAV_BODY_LOGGED_IN : DANMU_INFO_BODY;
      return new Response(JSON.stringify(body), { status: 200 });
    });

    const server = await fetchDanmuServer({
      roomId: 22748536,
      buvid: 'BUVID3VALUE',
      // 占位值：SESSDATA 是登录态，不要写真实值进仓库
      sessdata: 'FAKE-SESSDATA%2C1800000000%2Cabc12%2Adef',
      fetch,
      now: 1791548900000,
    });

    expect(server.uid).toBe(123456);
    const cookies = fetch.mock.calls.map((call) => (call[1]?.headers as Record<string, string>).Cookie);
    expect(cookies).toEqual([
      'buvid3=BUVID3VALUE; SESSDATA=FAKE-SESSDATA%2C1800000000%2Cabc12%2Adef',
      'buvid3=BUVID3VALUE; SESSDATA=FAKE-SESSDATA%2C1800000000%2Cabc12%2Adef',
    ]);
  });

  it('nav 没返回 wbi_img（Cookie 失效/风控）时报明确的错', async () => {
    const fetch = vi.fn(async () => new Response(JSON.stringify({ code: -352, data: null }), { status: 200 }));

    await expect(
      fetchDanmuServer({ roomId: 22748536, buvid: 'BUVID', sessdata: 'BAD', fetch, now: 1791548900000 }),
    ).rejects.toThrow(/nav 未返回 wbi_img/);
  });
});
