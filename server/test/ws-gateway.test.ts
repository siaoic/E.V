// WS 统一网关集成测试：真端口 + ws 客户端，附录 B 语义逐项验证

import { afterAll, beforeAll, describe, expect, it } from "vitest";
import type { AddressInfo } from "node:net";
import WebSocket from "ws";

import { makeFixtureRepo, makeTestApp } from "./helpers.js";

interface Frame {
  [key: string]: unknown;
}

class WsTestClient {
  private frames: Frame[] = [];
  private waiters: Array<{ test: (frame: Frame) => boolean; resolve: (frame: Frame) => void }> = [];
  private ws: WebSocket;

  constructor(url: string, headers: Record<string, string> = {}) {
    this.ws = new WebSocket(url, { headers });
    this.ws.on("message", (data) => {
      const frame = JSON.parse(String(data)) as Frame;
      const waiterIndex = this.waiters.findIndex((waiter) => waiter.test(frame));
      if (waiterIndex >= 0) {
        const waiter = this.waiters.splice(waiterIndex, 1)[0]!;
        waiter.resolve(frame);
      } else {
        this.frames.push(frame);
      }
    });
  }

  static open(url: string, headers: Record<string, string> = {}): WsTestClient {
    return new WsTestClient(url, headers);
  }

  get socket(): WebSocket {
    return this.ws;
  }

  async opened(): Promise<void> {
    if (this.ws.readyState === this.ws.OPEN) {
      return;
    }
    await new Promise<void>((resolve, reject) => {
      this.ws.once("open", resolve);
      this.ws.once("error", reject);
    });
  }

  async waitClose(): Promise<{ code: number; reason: string }> {
    return new Promise((resolve) => {
      this.ws.once("close", (code, reason) => resolve({ code, reason: reason.toString() }));
    });
  }

  send(frame: Frame | Record<string, unknown>): void {
    this.ws.send(JSON.stringify(frame));
  }

  /** 等待匹配的帧（先查缓冲，再挂等待器）；未匹配帧进缓冲。 */
  next(timeoutMs = 3000, test: (frame: Frame) => boolean = () => true): Promise<Frame> {
    const bufferedIndex = this.frames.findIndex(test);
    if (bufferedIndex >= 0) {
      return Promise.resolve(this.frames.splice(bufferedIndex, 1)[0]!);
    }
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error("等待 WS 帧超时")), timeoutMs);
      this.waiters.push({
        test,
        resolve: (frame) => {
          clearTimeout(timer);
          resolve(frame);
        },
      });
    });
  }

  async responseFor(id: string, timeoutMs = 3000): Promise<Frame> {
    return this.next(timeoutMs, (frame) => frame.op === "response" && frame.id === id);
  }

  close(): void {
    this.ws.close();
  }
}

describe("WS 统一网关", () => {
  const repo = makeFixtureRepo();
  const { app, tokenManager, wsTokens, db, logBuffer, wsGateway } = makeTestApp({ repo });
  const sessionToken = tokenManager.getToken();

  let baseUrl = "";
  const closeFns: Array<() => void> = [];

  beforeAll(async () => {
    db.sqlite
      .prepare(
        `INSERT INTO maisaka_monitor_events (event_type, session_id, timestamp, schema_version, payload_json, created_at)
         VALUES ('stage_round', 's1', 1726600000.0, 1, '{"round":1}', '2026-09-19 00:00:00.000000')`,
      )
      .run();
    await app.listen({ port: 0, host: "127.0.0.1" });
    const address = app.server.address() as AddressInfo;
    baseUrl = `ws://127.0.0.1:${address.port}/ws`;
  });

  afterAll(async () => {
    for (const fn of closeFns) fn();
    await app.close();
    db.close();
  });

  function authenticatedClient(): WsTestClient {
    const tokenRes = app.inject({
      method: "GET",
      url: "/api/webui/ws-token",
      cookies: { maibot_session: sessionToken },
    });
    return tokenRes.then((tokenResBody) => {
      const { token } = tokenResBody.json() as { token: string };
      const client = WsTestClient.open(`${baseUrl}?token=${token}`);
      closeFns.push(() => client.close());
      return client.opened().then(() =>
        client
          .next(3000, (frame) => frame.op === "event" && frame.domain === "system")
          .then((ready) => {
            expect(ready.event).toBe("ready");
            expect(typeof (ready.data as Record<string, unknown>).connection_id).toBe("string");
            expect(typeof (ready.data as Record<string, unknown>).timestamp).toBe("number");
            return client;
          }),
      );
    });
  }

  it("鉴权失败：无 token 无 Cookie → close 4001「认证失败，请重新登录」", async () => {
    const client = WsTestClient.open(baseUrl);
    const closeInfo = await client.waitClose();
    expect(closeInfo.code).toBe(4001);
    expect(closeInfo.reason).toBe("认证失败，请重新登录");
  });

  it("无效 JSON → invalid_message；未知 op → unsupported_operation", async () => {
    const client = await authenticatedClient();

    client.socket.send("not-json");
    const invalid = await client.responseFor("");
    expect(invalid.ok).toBe(false);
    expect((invalid.error as { code: string }).code).toBe("invalid_message");

    client.send({ op: "magic", id: "x1" });
    const unsupported = await client.responseFor("x1");
    expect((unsupported.error as { code: string }).code).toBe("unsupported_operation");
    client.close();
  });

  it("ping → pong（秒级 float ts）；logs:main 订阅回放 + 实时广播", async () => {
    const client = await authenticatedClient();

    client.send({ op: "ping", id: "p1" });
    const pongFrame = await client.next(3000, (frame) => frame.op === "pong");
    expect(typeof pongFrame.ts).toBe("number");

    logBuffer.push({ level: 30, msg: "测试日志一" });
    logBuffer.push({ level: 30, msg: "测试日志二" });

    client.send({ op: "subscribe", id: "s1", domain: "logs", topic: "main", data: { replay: 200 } });
    const response = await client.responseFor("s1");
    expect(response.ok).toBe(true);

    const snapshot = await client.next(3000, (frame) => frame.op === "event" && frame.domain === "logs");
    expect(snapshot.event).toBe("snapshot");
    const entries = (snapshot.data as { entries: Array<Record<string, unknown>> }).entries;
    expect(entries.length).toBeGreaterThanOrEqual(2);

    logBuffer.push({ level: 30, msg: "实时日志" });
    const live = await client.next(3000, (frame) => frame.op === "event" && frame.domain === "logs" && frame.event !== "snapshot");
    expect((live.data as { msg: string }).msg).toBe("实时日志");
  });

  it("订阅校验：不支持的组合 → unsupported_subscription；退订缺参 → invalid_unsubscribe", async () => {
    const client = await authenticatedClient();

    client.send({ op: "subscribe", id: "b1", domain: "chat", topic: "main" });
    const bad = await client.responseFor("b1");
    expect((bad.error as { code: string }).code).toBe("unsupported_subscription");

    client.send({ op: "unsubscribe", id: "b2" });
    const invalid = await client.responseFor("b2");
    expect((invalid.error as { code: string }).code).toBe("invalid_unsubscribe");
    client.close();
  });

  it("maisaka_monitor:main：DB 回放 + stage.snapshot 收尾", async () => {
    const client = await authenticatedClient();
    client.send({ op: "subscribe", id: "m1", domain: "maisaka_monitor", topic: "main" });
    const response = await client.responseFor("m1");
    expect(response.ok).toBe(true);

    const replayed = await client.next(3000, (frame) => frame.op === "event" && frame.event === "stage_round");
    expect((replayed.data as { payload: { round: number } }).payload.round).toBe(1);

    const stage = await client.next(3000, (frame) => frame.event === "stage.snapshot");
    expect(stage.domain).toBe("maisaka_monitor");
    expect(typeof (stage.data as { timestamp: number }).timestamp).toBe("number");
  });

  it("plugin_progress:main：快照返回进度注册表", async () => {
    wsGateway!.setPluginProgress("install-demo", { percent: 42 });
    const client = await authenticatedClient();
    client.send({ op: "subscribe", id: "pp1", domain: "plugin_progress", topic: "main" });
    const response = await client.responseFor("pp1");
    expect(response.ok).toBe(true);
    const snapshot = await client.next(3000, (frame) => frame.op === "event" && frame.event === "snapshot");
    expect((snapshot.data as { progress: Record<string, unknown> }).progress["install-demo"]).toEqual({ percent: 42 });
  });

  it("chat 域：open/send/nickname/close 全流程 + 错误码", async () => {
    const client = await authenticatedClient();

    // 不支持的域
    client.send({ op: "call", id: "c0", domain: "logs", method: "whatever" });
    const badDomain = await client.responseFor("c0");
    expect((badDomain.error as { code: string }).code).toBe("unsupported_domain");

    // open 缺 session
    client.send({ op: "call", id: "c1", domain: "chat", method: "session.open", data: {} });
    const missing = await client.responseFor("c1");
    expect((missing.error as { code: string }).code).toBe("missing_session");

    // 正常 open
    client.send({
      op: "call",
      id: "c2",
      domain: "chat",
      method: "session.open",
      session: "s1",
      data: { user_name: "阿明" },
    });
    const opened = await client.responseFor("c2");
    expect(opened.ok).toBe(true);
    const openData = opened.data as { session: string; session_id: string; client_mode: string };
    expect(openData.session).toBe("s1");
    expect(openData.session_id).toMatch(/^[0-9a-f]+:s1$/);
    expect(openData.client_mode).toBe("webui");

    // message.send 未打开的会话
    client.send({ op: "call", id: "c3", domain: "chat", method: "message.send", session: "s2", data: { content: "hi" } });
    const notFound = await client.responseFor("c3");
    expect((notFound.error as { code: string }).code).toBe("session_not_found");

    // 已打开会话发消息
    client.send({
      op: "call",
      id: "c4",
      domain: "chat",
      method: "message.send",
      session: "s1",
      data: { content: "你好", images: [], user_name: "阿明" },
    });
    const accepted = await client.responseFor("c4");
    expect(accepted.data).toEqual({ accepted: true, session: "s1" });

    // update_nickname
    client.send({
      op: "call",
      id: "c5",
      domain: "chat",
      method: "session.update_nickname",
      session: "s1",
      data: { user_name: "大明" },
    });
    const renamed = await client.responseFor("c5");
    expect(renamed.data).toEqual({ session: "s1", user_name: "大明" });

    // 不支持的方法
    client.send({ op: "call", id: "c6", domain: "chat", method: "explode", session: "s1" });
    const badMethod = await client.responseFor("c6");
    expect((badMethod.error as { code: string }).code).toBe("unsupported_method");

    // close
    client.send({ op: "call", id: "c7", domain: "chat", method: "session.close", session: "s1" });
    const closed = await client.responseFor("c7");
    expect(closed.data).toEqual({ session: "s1" });

    // 关闭后再发 → session_not_found
    client.send({ op: "call", id: "c8", domain: "chat", method: "message.send", session: "s1", data: {} });
    const afterClose = await client.responseFor("c8");
    expect((afterClose.error as { code: string }).code).toBe("session_not_found");
  });

  it("连接断开清理订阅：断开后订阅计数回落", async () => {
    const client = await authenticatedClient();
    const before = wsGateway!.getSubscriptionCount("logs:main");
    client.send({ op: "subscribe", id: "d1", domain: "logs", topic: "main" });
    await client.responseFor("d1");
    expect(wsGateway!.getSubscriptionCount("logs:main")).toBe(before + 1);
    client.close();
    await new Promise((resolve) => setTimeout(resolve, 200));
    expect(wsGateway!.getSubscriptionCount("logs:main")).toBe(before);
  });
});
