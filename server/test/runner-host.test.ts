// Runner Host 集成测试：以 TS 模拟 Runner 客户端（真 TCP + MsgPack 信封）验证
// 握手校验、runner.ready、capability 分派与 unavailable 显式回错、关停顺序

import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { connect, type Socket } from "node:net";
import { decode, encode } from "@msgpack/msgpack";

import { silentLogger } from "./helpers.js";
import {
  decodeEnvelope,
  encodeEnvelope,
  type Envelope,
} from "../src/kernel/runner-host/protocol.js";
import { RunnerHost } from "../src/kernel/runner-host/supervisor.js";

const FIXTURE_REPO = process.env.MAIBOT_ROOT ?? process.cwd().replace(/server$/, "");

/** 模拟 Runner：连上 Host，发 runner.hello，等待 ready 应答。 */
class FakeRunner {
  private socket: Socket | null = null;
  private buffer = Buffer.alloc(0);
  readonly frames: Envelope[] = [];

  connect(port: number): Promise<void> {
    return new Promise((resolve, reject) => {
      this.socket = connect({ port, host: "127.0.0.1" }, () => resolve());
      this.socket.on("error", reject);
      this.socket.on("data", (chunk) => {
        this.buffer = Buffer.concat([this.buffer, chunk]);
        while (this.buffer.length >= 4) {
          const length = this.buffer.readUInt32BE(0);
          if (this.buffer.length < 4 + length) break;
          const payload = this.buffer.subarray(4, 4 + length);
          this.buffer = this.buffer.subarray(4 + length);
          this.frames.push(decodeEnvelope(new Uint8Array(payload)));
        }
      });
    });
  }

  rawSend(data: Buffer): void {
    this.socket!.write(data);
  }

  sendRequest(requestId: number, method: string, payload: Record<string, unknown>): void {
    const bytes = encodeEnvelope({
      protocol_version: "1.0.0",
      request_id: requestId,
      message_type: "request",
      method,
      plugin_id: "",
      timestamp_ms: Date.now(),
      timeout_ms: 5000,
      payload,
      error: null,
    });
    const header = Buffer.alloc(4);
    header.writeUInt32BE(bytes.length, 0);
    this.socket!.write(Buffer.concat([header, Buffer.from(bytes)]));
  }

  async waitFor(test: (frame: Envelope) => boolean, timeoutMs = 3000): Promise<Envelope> {
    const started = Date.now();
    while (Date.now() - started < timeoutMs) {
      const found = this.frames.find(test);
      if (found) return found;
      await new Promise((resolve) => setTimeout(resolve, 20));
    }
    throw new Error("等待 Runner 帧超时");
  }

  close(): void {
    this.socket?.destroy();
  }
}

describe("RunnerHost", () => {
  let host: RunnerHost;
  const capabilities: Record<string, (args: Record<string, unknown>) => Record<string, unknown>> = {
    "config.get": (args) => ({ success: true, value: `mock:${String(args.key)}` }),
  };

  beforeAll(async () => {
    host = new RunnerHost({
      rootDir: FIXTURE_REPO,
      hostVersion: "1.2.5",
      pluginDirs: ["plugins"],
      skipReadyWait: true,
      capabilities,
      logger: silentLogger,
    });
    await host.start();
  });

  afterAll(async () => {
    await host.stop();
  });

  function connectFakeRunner(): FakeRunner {
    const runner = new FakeRunner();
    return runner;
  }

  it("握手：正确 token + 合法 SDK 版本 → accepted=true 且带 host_version", async () => {
    const runner = connectFakeRunner();
    await runner.connect(host.port);
    runner.sendRequest(1, "runner.hello", {
      runner_id: "fake-runner",
      sdk_version: "2.5.0",
      session_token: host.sessionToken,
    });
    const helloResp = await runner.waitFor((frame) => frame.message_type === "response");
    expect(helloResp.payload.accepted).toBe(true);
    expect((helloResp.payload as Record<string, unknown>).host_version).toBe("1.2.5");
    runner.close();
    await new Promise((resolve) => setTimeout(resolve, 100));
  });

  it("握手：错误 token → accepted=false 且原因「会话令牌无效」", async () => {
    const runner = connectFakeRunner();
    await runner.connect(host.port);
    runner.sendRequest(1, "runner.hello", {
      runner_id: "fake-runner",
      sdk_version: "2.5.0",
      session_token: "wrong-token",
    });
    const resp = await runner.waitFor((frame) => frame.message_type === "response");
    expect(resp.payload.accepted).toBe(false);
    expect(resp.payload.reason).toBe("会话令牌无效");
    runner.close();
  });

  it("握手：SDK 版本越界（3.0.0）→ accepted=false", async () => {
    const runner = connectFakeRunner();
    await runner.connect(host.port);
    runner.sendRequest(1, "runner.hello", {
      runner_id: "fake-runner",
      sdk_version: "3.0.0",
      session_token: host.sessionToken,
    });
    const resp = await runner.waitFor((frame) => frame.message_type === "response");
    expect(resp.payload.accepted).toBe(false);
    expect(String(resp.payload.reason)).toContain("SDK 版本");
    runner.close();
  });

  it("runner.ready + capability 分派：已注册能力命中、未注册显式 unavailable", async () => {
    const runner = connectFakeRunner();
    await runner.connect(host.port);
    runner.sendRequest(1, "runner.hello", {
      runner_id: "fake-runner",
      sdk_version: "2.5.0",
      session_token: host.sessionToken,
    });
    await runner.waitFor((frame) => frame.message_type === "response" && frame.payload.accepted === true);

    // runner.ready
    runner.sendRequest(2, "runner.ready", {
      loaded_plugins: ["maibot-live.world-bilibili"],
      failed_plugins: [],
      failed_plugin_reasons: {},
      inactive_plugins: [],
    });
    await new Promise((resolve) => setTimeout(resolve, 100));
    expect(host.ready.loaded).toEqual(["maibot-live.world-bilibili"]);

    // 已注册 capability
    runner.sendRequest(3, "cap.call", { capability: "config.get", args: { key: "bot.nickname" } });
    const capResp = await runner.waitFor((frame) => frame.message_type === "response" && frame.request_id === 3);
    expect(capResp.payload).toEqual({ success: true, value: "mock:bot.nickname" });

    // 未注册 capability → capability_unavailable
    runner.sendRequest(4, "cap.call", { capability: "maisaka.context.append", args: {} });
    const unavailable = await runner.waitFor((frame) => frame.message_type === "response" && frame.request_id === 4);
    expect((unavailable.error as { code: string }).code).toBe("capability_unavailable");

    // Host → Runner 调用：request_id 关联；FakeRunner 回应 response 信封
    const pendingCall = host.callRunner("plugin.invoke", { component_name: "x" }, 3000).catch((error) => ({ error: String(error) }));
    await runner.waitFor(
      (frame) => frame.message_type === "request" && frame.method === "plugin.invoke",
      3000,
    );
    const hostRequest = runner.frames.filter((frame) => frame.message_type === "request").at(-1)!;
    const responseBytes = encodeEnvelope({
      protocol_version: "1.0.0",
      request_id: hostRequest.request_id,
      message_type: "response",
      method: "plugin.invoke",
      plugin_id: "",
      timestamp_ms: Date.now(),
      timeout_ms: 5000,
      payload: { success: true, result: "done" },
      error: null,
    });
    const header = Buffer.alloc(4);
    header.writeUInt32BE(responseBytes.length, 0);
    runner.rawSend(Buffer.concat([header, Buffer.from(responseBytes)]));
    const result = await pendingCall;
    expect(result).toEqual({ success: true, result: "done" });

    runner.close();
  });
});
