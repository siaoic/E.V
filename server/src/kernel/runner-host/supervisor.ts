/**
 * 插件 Runner 的 TS 侧 Host（对照 src/plugin_runtime/host/supervisor.py + rpc_server.py）。
 *
 * 契约（逐字对齐，插件生态红线 R1/R3）：
 * - 传输：TCP 监听 127.0.0.1 临时端口（Runner 端按地址含 ':' 自动选 TCP 传输）；
 * - 分帧：4 字节大端长度前缀 + MsgPack 信封；
 * - 环境变量契约（_build_runner_environment 逐键复刻）；
 * - 握手：Runner 主动发 runner.hello（runner_id/sdk_version/session_token），
 *   Host 校验 token 与 SDK 区间 [1.0.0, 2.99.99] 后应答 HelloResponsePayload；
 * - 未注册的 capability 统一显式回 capability_unavailable（不静默吞）。
 *
 * 已知边界：本批只挂载无引擎依赖的 capability 子集（config/tool/component/render/
 * knowledge/statistics 等由调用方注册）；maisaka 与 message 两族的 capability 依赖对话引擎的
 * capability 在 D1 二期接入。
 */

import { spawn, type ChildProcess } from "node:child_process";
import { createServer, type Server, type Socket } from "node:net";
import { randomBytes, randomUUID } from "node:crypto";

import type { FastifyBaseLogger } from "fastify";

import {
  decodeEnvelope,
  encodeEnvelope,
  makeResponse,
  PROTOCOL_VERSION,
  MAX_SDK_VERSION,
  MIN_SDK_VERSION,
  type Envelope,
} from "./protocol.js";

export interface RunnerReadyPayload {
  loaded_plugins: string[];
  failed_plugins: string[];
  failed_plugin_reasons: Record<string, string>;
  inactive_plugins: string[];
}

export type CapabilityHandler = (args: Record<string, unknown>) => Promise<Record<string, unknown>> | Record<string, unknown>;

export interface SupervisorOptions {
  /** 仓库根（决定 python -m 的 cwd 与 plugins/ 目录）。 */
  rootDir: string;
  hostVersion: string;
  pluginDirs: string[];
  runnerGroup?: string;
  pluginTypeFilter?: string;
  trustedPluginDirs?: string[];
  blockedPluginReasons?: Record<string, string>;
  externalPluginIds?: Record<string, string>;
  pythonExecutable?: string;
  /** 测试用：只监听并等待 FakeRunner 自行接入，不等待 runner.ready。 */
  skipReadyWait?: boolean;
  capabilities: Record<string, CapabilityHandler>;
  logger: FastifyBaseLogger;
  onRunnerExit?: (code: number | null) => void;
}

function compareVersions(left: string, right: string): number {
  const parse = (value: string) => value.split(".").map((part) => Number.parseInt(part, 10) || 0);
  const a = parse(left);
  const b = parse(right);
  const length = Math.max(a.length, b.length);
  for (let i = 0; i < length; i += 1) {
    const diff = (a[i] ?? 0) - (b[i] ?? 0);
    if (diff !== 0) {
      return diff < 0 ? -1 : 1;
    }
  }
  return 0;
}

function inVersionRange(version: string, min: string, max: string): boolean {
  return compareVersions(version, min) >= 0 && compareVersions(version, max) <= 0;
}

interface PendingRequest {
  resolve: (payload: Record<string, unknown>) => void;
  reject: (error: Error) => void;
  timer: NodeJS.Timeout;
}

export class RunnerHost {
  readonly sessionId = randomUUID().slice(0, 8);
  readonly sessionToken = randomBytes(32).toString("hex");

  private server: Server | null = null;
  private port = 0;
  private runnerProcess: ChildProcess | null = null;
  private socket: Socket | null = null;
  private buffer = Buffer.alloc(0);
  private nextRequestId = 100;
  private pending = new Map<number, PendingRequest>();
  private connected = false;

  readonly ready: { loaded: string[]; failed: string[]; failedReasons: Record<string, string>; inactive: string[] } = {
    loaded: [],
    failed: [],
    failedReasons: {},
    inactive: [],
  };
  private readyWaiters: Array<() => void> = [];

  constructor(private readonly options: SupervisorOptions) {}

  // ------------------------------------------------------------------ 生命周期

  async start(): Promise<void> {
    await this.listen();
    if (this.options.skipReadyWait) {
      return;
    }
    this.spawnRunner();
    await this.waitForReady();
  }

  async stop(): Promise<void> {
    // 优雅关停：已连接时先发 shutdown（drain），随后销毁连接并关 server
    if (this.connected && this.socket) {
      try {
        await this.callRunner("plugin.shutdown", { reason: "host_shutdown" }, 3000);
      } catch {
        // Runner 已死则忽略
      }
    }
    if (this.runnerProcess && this.runnerProcess.exitCode === null) {
      this.runnerProcess.kill();
    }
    if (this.socket) {
      this.socket.destroy();
      this.socket = null;
    }
    this.connected = false;
    if (this.server) {
      await new Promise<void>((resolve) => this.server!.close(() => resolve()));
      this.server = null;
    }
    for (const [id, pending] of this.pending) {
      pending.reject(new Error("Host 正在关停"));
      this.pending.delete(id);
    }
    this.socket = null;
    this.connected = false;
  }

  private async listen(): Promise<void> {
    this.server = createServer((socket) => this.onConnection(socket));
    await new Promise<void>((resolve, reject) => {
      this.server!.once("error", reject);
      this.server!.listen(0, "127.0.0.1", () => resolve());
    });
    this.port = (this.server.address() as { port: number }).port;
    this.options.logger.info({ port: this.port }, "Runner RPC 服务端已监听");
  }

  private spawnRunner(): void {
    const env: Record<string, string> = {
      ...process.env as Record<string, string>,
      // _build_runner_environment 逐键复刻
      MAIBOT_IPC_ADDRESS: `127.0.0.1:${this.port}`,
      MAIBOT_SESSION_TOKEN: this.sessionToken,
      MAIBOT_PLUGIN_DIRS: this.options.pluginDirs.join(path_sep()),
      MAIBOT_HOST_VERSION: this.options.hostVersion,
      MAIBOT_RUNNER_GROUP: this.options.runnerGroup ?? "main",
      MAIBOT_BLOCKED_PLUGIN_REASONS: JSON.stringify(this.options.blockedPluginReasons ?? {}),
      MAIBOT_EXTERNAL_PLUGIN_IDS: JSON.stringify(this.options.externalPluginIds ?? {}),
      MAIBOT_PLUGIN_TYPE_FILTER: this.options.pluginTypeFilter ?? "",
      MAIBOT_TRUSTED_PLUGIN_DIRS: (this.options.trustedPluginDirs ?? []).join(path_sep()),
    };
    const executable = this.options.pythonExecutable ?? "python";
    this.runnerProcess = spawn(executable, ["-m", "src.plugin_runtime.runner.runner_main"], {
      cwd: this.options.rootDir,
      env,
      stdio: ["ignore", "ignore", "pipe"],
    });
    this.runnerProcess.stderr?.on("data", (chunk: Buffer) => {
      for (const line of chunk.toString("utf-8").split("\n")) {
        if (line.trim()) {
          this.options.logger.info({ runner: line }, "[runner-stderr]");
        }
      }
    });
    this.runnerProcess.on("exit", (code) => {
      this.options.logger.warn({ code }, "Runner 进程退出");
      this.options.onRunnerExit?.(code);
    });
  }

  private waitForReady(): Promise<void> {
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error("Runner 就绪超时（runner.ready 未到达）")), 120_000);
      this.readyWaiters.push(() => {
        clearTimeout(timer);
        resolve();
      });
    });
  }

  private signalReady(): void {
    for (const waiter of this.readyWaiters) {
      waiter();
    }
    this.readyWaiters = [];
  }

  private markReady(payload: Record<string, unknown>): void {
    this.ready.loaded = (payload.loaded_plugins as string[] | undefined) ?? [];
    this.ready.failed = (payload.failed_plugins as string[] | undefined) ?? [];
    this.ready.failedReasons = (payload.failed_plugin_reasons as Record<string, string> | undefined) ?? {};
    this.ready.inactive = (payload.inactive_plugins as string[] | undefined) ?? [];
    this.signalReady();
  }

  // ------------------------------------------------------------------ 连接与信封路由

  private onConnection(socket: Socket): void {
    this.socket = socket;
    this.buffer = Buffer.alloc(0);

    socket.on("data", (chunk: Buffer) => {
      this.buffer = Buffer.concat([this.buffer, chunk]);
      while (this.buffer.length >= 4) {
        const length = this.buffer.readUInt32BE(0);
        if (this.buffer.length < 4 + length) {
          break;
        }
        const payloadBytes = this.buffer.subarray(4, 4 + length);
        this.buffer = this.buffer.subarray(4 + length);
        this.onEnvelope(payloadBytes);
      }
    });
    socket.on("error", (error) => this.options.logger.error({ error }, "Runner 连接错误"));
    socket.on("close", () => {
      this.connected = false;
      this.options.logger.info("Runner 连接已关闭");
    });
  }

  private onEnvelope(payloadBytes: Buffer): void {
    let envelope: Envelope;
    try {
      envelope = decodeEnvelope(new Uint8Array(payloadBytes));
    } catch (error) {
      this.options.logger.error({ error }, "协议违约：无法解码 Runner 信封");
      return;
    }

    if (envelope.message_type === "request") {
      if (envelope.method === "runner.hello") {
        this.handleHello(envelope);
        return;
      }
      if (envelope.method === "runner.ready") {
        this.markReady(envelope.payload);
        this.sendResponse(envelope, { accepted: true });
        return;
      }
      if (envelope.method === "cap.call") {
        void this.handleCapabilityCall(envelope);
        return;
      }
      if (envelope.method === "runner.log_batch") {
        this.sendResponse(envelope, { accepted: true });
        return;
      }
      // 未注册的 Host 方法：显式回错，不静默
      this.sendResponse(envelope, undefined, {
        code: "method_not_found",
        message: `Host 未实现方法: ${envelope.method}`,
      });
      return;
    }

    // response：匹配待处理请求
    const pending = this.pending.get(envelope.request_id);
    if (pending) {
      this.pending.delete(envelope.request_id);
      clearTimeout(pending.timer);
      if (envelope.error) {
        pending.reject(new Error(`${envelope.error.code}: ${envelope.error.message}`));
      } else {
        pending.resolve(envelope.payload);
      }
    }
  }

  private handleHello(envelope: Envelope): void {
    const payload = envelope.payload as Record<string, unknown>;
    const sdkVersion = String(payload.sdk_version ?? "");
    const sessionToken = String(payload.session_token ?? "");

    let accepted = true;
    let reason = "";
    if (sessionToken !== this.sessionToken) {
      accepted = false;
      reason = "会话令牌无效";
    } else if (this.connected) {
      accepted = false;
      reason = "已有活跃 Runner 连接，拒绝新的握手";
    } else if (!inVersionRange(sdkVersion, MIN_SDK_VERSION, MAX_SDK_VERSION)) {
      accepted = false;
      reason = `SDK 版本 ${sdkVersion} 不在支持范围 [${MIN_SDK_VERSION}, ${MAX_SDK_VERSION}]`;
    }

    if (accepted) {
      this.connected = true;
    }
    this.options.logger[accepted ? "info" : "error"](
      `握手${accepted ? "接受" : "拒绝"}: runner_id=${payload.runner_id} sdk=${sdkVersion} ${reason}`,
    );
    this.sendRaw(
      encodeEnvelope({
        protocol_version: PROTOCOL_VERSION,
        request_id: envelope.request_id,
        message_type: "response",
        method: envelope.method,
        plugin_id: "",
        timestamp_ms: Date.now(),
        timeout_ms: envelope.timeout_ms,
        payload: { accepted, host_version: accepted ? this.options.hostVersion : "", reason },
        error: null,
      }),
    );
  }

  private async handleCapabilityCall(envelope: Envelope): Promise<void> {
    const capability = String(envelope.payload.capability ?? "");
    const args = (envelope.payload.args as Record<string, unknown> | undefined) ?? {};
    const handler = this.options.capabilities[capability];
    if (!handler) {
      this.sendResponse(envelope, undefined, {
        code: "capability_unavailable",
        message: `能力未实现或未迁移: ${capability}`,
        details: { capability },
      });
      return;
    }
    try {
      const result = await handler(args);
      this.sendResponse(envelope, result ?? {});
    } catch (error) {
      this.sendResponse(envelope, undefined, {
        code: "capability_error",
        message: String(error instanceof Error ? error.message : error),
      });
    }
  }

  private sendResponse(request: Envelope, payload?: Record<string, unknown>, error?: { code: string; message: string; details?: Record<string, unknown> }): void {
    this.sendRaw(encodeEnvelope(makeResponse(request, payload, error)));
  }

  private sendRaw(bytes: Uint8Array): void {
    const socket = this.socket;
    if (!socket) {
      return;
    }
    const header = Buffer.alloc(4);
    header.writeUInt32BE(bytes.length, 0);
    socket.write(Buffer.concat([header, Buffer.from(bytes)]));
  }

  // ------------------------------------------------------------------ Host → Runner 调用

  async callRunner(method: string, payload: Record<string, unknown>, timeoutMs = 30_000): Promise<Record<string, unknown>> {
    const requestId = this.nextRequestId++;
    const envelope = encodeEnvelope({
      protocol_version: PROTOCOL_VERSION,
      request_id: requestId,
      message_type: "request",
      method,
      plugin_id: "",
      timestamp_ms: Date.now(),
      timeout_ms: timeoutMs,
      payload,
      error: null,
    });
    this.sendRaw(envelope);
    return new Promise<Record<string, unknown>>((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(requestId);
        reject(new Error(`Runner 调用超时: ${method}`));
      }, timeoutMs);
      this.pending.set(requestId, { resolve, reject, timer });
    });
  }
}

function path_sep(): string {
  return process.platform === "win32" ? ";" : ":";
}
