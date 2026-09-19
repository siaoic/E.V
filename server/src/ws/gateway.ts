/**
 * 统一 WS 网关（对照 src/webui/routers/websocket/{unified,manager,auth}.py，
 * 协议见迁移调研附录 B——字段名逐字对齐，前端 dashboard 按此解析）。
 *
 * 语义清单：
 * - 建连鉴权：?token= 一次性临时 token 或 Cookie 回退；失败 close(4001,
 *   "认证失败，请重新登录")；成功立即下发 system/ready；
 * - 客户端 op：ping / subscribe / unsubscribe / call（仅 chat 域）；
 * - 服务端 op：response / event / pong（ts 为秒级 float）；
 * - 订阅仅三种：logs:main、plugin_progress:main、maisaka_monitor:main；
 * - per-connection 出站队列（背压不阻塞事件循环），断开清理订阅与会话。
 */

import { randomUUID } from "node:crypto";

import type { FastifyInstance, FastifyReply } from "fastify";
import type { RawData, WebSocket as WsSocket } from "ws";

import type { FastifyBaseLogger } from "fastify";

import type { DbHandle } from "../db/client.js";
import type { TokenManager } from "../auth/token-manager.js";
import type { WsTokenStore } from "../auth/ws-tokens.js";
import { COOKIE_NAME } from "../auth/cookies.js";

export const WS_CLOSE_AUTH_FAILED = 4001;
export const WS_AUTH_FAILED_REASON = "认证失败，请重新登录";

const SUBSCRIPTION_KEYS = new Set(["logs:main", "plugin_progress:main", "maisaka_monitor:main"]);
const HIGH_WATER_MARK = 1000;

export interface OutboundFrame {
  op: "response" | "event" | "pong";
  [field: string]: unknown;
}

interface Connection {
  id: string;
  socket: WsSocket;
  queue: string[];
  draining: boolean;
  dropped: boolean;
  subscriptions: Set<string>;
  chatSessions: Map<string, { sessionId: string; userName: string }>;
}

function nowFloatSeconds(): number {
  return Date.now() / 1000;
}

function errorBody(code: string, message: string): { code: string; message: string } {
  return { code, message };
}

export class WsGateway {
  readonly connections = new Map<string, Connection>();
  readonly subscriptionIndex = new Map<string, Set<string>>();
  readonly pluginProgress = new Map<string, unknown>();

  constructor(
    private readonly options: {
      tokenManager: TokenManager;
      wsTokens: WsTokenStore;
      db: DbHandle | null;
      logger: FastifyBaseLogger;
    },
  ) {}

  private get tokenManager(): TokenManager {
    return this.options.tokenManager;
  }

  private get wsTokens(): WsTokenStore {
    return this.options.wsTokens;
  }

  private get db(): DbHandle | null {
    return this.options.db;
  }

  private get logger(): FastifyBaseLogger {
    return this.options.logger;
  }

  // ------------------------------------------------------------------ 连接生命周期

  authenticate(request: { url?: string; cookies?: Record<string, string | undefined> }): boolean {
    const url = request.url ?? "";
    const tokenParam = new URL(url.startsWith("/") ? `http://local${url}` : url).searchParams.get("token");
    if (tokenParam) {
      return this.wsTokens.consume(tokenParam, (sessionToken) => this.tokenManager.verifyToken(sessionToken));
    }
    const cookieToken = request.cookies?.[COOKIE_NAME];
    return typeof cookieToken === "string" && cookieToken !== "" && this.tokenManager.verifyToken(cookieToken);
  }

  registerConnection(id: string, socket: WsSocket): Connection {
    const connection: Connection = {
      id,
      socket,
      queue: [],
      draining: false,
      dropped: false,
      subscriptions: new Set(),
      chatSessions: new Map(),
    };
    this.connections.set(id, connection);
    return connection;
  }

  cleanupConnection(id: string): void {
    const connection = this.connections.get(id);
    if (connection) {
      for (const key of connection.subscriptions) {
        this.subscriptionIndex.get(key)?.delete(id);
        if ((this.subscriptionIndex.get(key)?.size ?? 0) === 0) {
          this.subscriptionIndex.delete(key);
        }
      }
      connection.subscriptions.clear();
      this.connections.delete(id);
    }
  }

  // ------------------------------------------------------------------ 出站（per-connection 队列）

  private enqueue(connection: Connection, frame: OutboundFrame): void {
    if (connection.dropped || connection.socket.readyState !== 1) {
      return;
    }
    connection.queue.push(JSON.stringify(frame));
    if (connection.queue.length > HIGH_WATER_MARK) {
      // 背压保护：消费过慢的连接直接断开（与「队列缓冲、不阻塞事件循环」同语义）
      connection.dropped = true;
      connection.queue = [];
      connection.socket.close(1011, "发送队列溢出");
      this.logger.warn({ connectionId: connection.id }, "WS 发送队列溢出，已断开连接");
      return;
    }
    if (connection.draining) {
      return;
    }
    connection.draining = true;
    const drain = () => {
      while (connection.queue.length > 0 && connection.socket.readyState === 1) {
        connection.socket.send(connection.queue.shift()!);
      }
      connection.draining = false;
      if (connection.queue.length > 0 && connection.socket.readyState === 1) {
        setImmediate(drain);
      }
    };
    setImmediate(drain);
  }

  sendResponse(connectionId: string, requestId: string | null, ok: boolean, data?: unknown, error?: { code: string; message: string }): void {
    const connection = this.connections.get(connectionId);
    if (!connection) {
      return;
    }
    const frame: OutboundFrame = { op: "response", id: requestId ?? "" };
    if (ok) {
      frame.ok = true;
      frame.data = data ?? {};
    } else {
      frame.ok = false;
      frame.error = error ?? { code: "unknown", message: "" };
    }
    this.enqueue(connection, frame);
  }

  async sendEvent(
    connectionId: string,
    domain: string,
    event: string,
    data: unknown,
    extra?: { topic?: string; session?: string },
  ): Promise<void> {
    const connection = this.connections.get(connectionId);
    if (!connection) {
      return;
    }
    const frame: OutboundFrame = { op: "event", domain, event, data };
    if (extra?.topic) {
      frame.topic = extra.topic;
    }
    if (extra?.session) {
      frame.session = extra.session;
    }
    this.enqueue(connection, frame);
  }

  broadcastToTopic(domain: string, topic: string, event: string, data: unknown): void {
    const key = `${domain}:${topic}`;
    for (const connectionId of this.subscriptionIndex.get(key) ?? []) {
      void this.sendEvent(connectionId, domain, event, data, { topic });
    }
  }

  /** 运行期进度登记（plugin_progress:main 的快照来源，进程内状态）。 */
  setPluginProgress(name: string, payload: unknown): void {
    this.pluginProgress.set(name, payload);
    this.broadcastToTopic("plugin_progress", "main", "update", { name, progress: payload });
  }

  /** 运行期日志推送（logs:main 的实时事件）。 */
  broadcastLog(entry: unknown): void {
    this.broadcastToTopic("logs", "main", "log", entry);
  }

  sendReady(connectionId: string): void {
    this.enqueue(this.connections.get(connectionId)!, {
      op: "event",
      domain: "system",
      event: "ready",
      data: { connection_id: connectionId, timestamp: nowFloatSeconds() },
    });
  }

  // ------------------------------------------------------------------ 订阅

  getSubscriptionCount(topicKey: string): number {
    return this.subscriptionIndex.get(topicKey)?.size ?? 0;
  }

  private subscribe(connection: Connection, domain: string, topic: string): { ok: true; replay: () => void } | { ok: false } {
    const key = `${domain}:${topic}`;
    if (!SUBSCRIPTION_KEYS.has(key)) {
      return { ok: false };
    }
    connection.subscriptions.add(key);
    const set = this.subscriptionIndex.get(key) ?? new Set<string>();
    set.add(connection.id);
    this.subscriptionIndex.set(key, set);
    return { ok: true, replay: () => this.replaySubscription(connection.id, domain, topic) };
  }

  private unsubscribe(connection: Connection, domain: string, topic: string): void {
    const key = `${domain}:${topic}`;
    connection.subscriptions.delete(key);
    const set = this.subscriptionIndex.get(key);
    if (set) {
      set.delete(connection.id);
      if (set.size === 0) {
        this.subscriptionIndex.delete(key);
      }
    }
  }

  /** 订阅成功后的快照/回放（附录 B.2 ②）。 */
  private replaySubscription(connectionId: string, domain: string, topic: string): void {
    if (domain === "logs") {
      const entries = this.logReplay?.(200) ?? [];
      void this.sendEvent(connectionId, domain, "snapshot", { entries }, { topic });
      return;
    }
    if (domain === "plugin_progress") {
      this.enqueue(this.connections.get(connectionId)!, {
        op: "event",
        domain,
        topic,
        event: "snapshot",
        data: { progress: Object.fromEntries(this.pluginProgress) },
      });
      return;
    }
    // maisaka_monitor:main：since_event_id 之后逐条回放 + stage.snapshot 收尾
    if (!this.db) {
      void this.sendEvent(connectionId, domain, "stage.snapshot", { entries: 0, timestamp: nowFloatSeconds() }, { topic });
      return;
    }
    const monitorRows = this.db.sqlite
      .prepare<[number, number], { event_id: number; event_type: string; session_id: string; timestamp: number; payload_json: string }>(
        "SELECT event_id, event_type, session_id, timestamp, payload_json FROM maisaka_monitor_events WHERE event_id > ? ORDER BY event_id ASC LIMIT ?",
      )
      .all(0, 1000);
    for (const row of monitorRows) {
      let payload: unknown = {};
      try {
        payload = JSON.parse(row.payload_json);
      } catch {
        payload = {};
      }
      void this.sendEvent(
        connectionId,
        domain,
        row.event_type,
        { event_id: row.event_id, session_id: row.session_id, timestamp: row.timestamp, payload },
        { topic },
      );
    }
    void this.sendEvent(connectionId, domain, "stage.snapshot", { entries: monitorRows.length, timestamp: nowFloatSeconds() }, { topic });
  }

  /** 日志回放源（logs:main 的快照数据），由 main.ts 装配。 */
  logReplay: ((limit: number) => unknown[]) | null = null;

  // ------------------------------------------------------------------ 消息处理

  handleMessage(connectionId: string, data: RawData): void {
    const connection = this.connections.get(connectionId);
    if (!connection) {
      return;
    }
    let message: Record<string, unknown>;
    try {
      const text = typeof data === "string" ? data : data.toString();
      const parsed = JSON.parse(text) as unknown;
      if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
        throw new Error("not an object");
      }
      message = parsed as Record<string, unknown>;
    } catch {
      this.sendResponse(connectionId, null, false, undefined, errorBody("invalid_message", "消息不是合法的 JSON 对象"));
      return;
    }

    const requestId = typeof message.id === "string" ? message.id : null;
    const op = String(message.op ?? "");

    if (op === "ping") {
      this.enqueue(connection, { op: "pong", ts: nowFloatSeconds() });
      return;
    }

    if (op === "subscribe") {
      const domain = String(message.domain ?? "");
      const topic = String(message.topic ?? "");
      const result = this.subscribe(connection, domain, topic);
      if (!result.ok) {
        this.sendResponse(connectionId, requestId, false, undefined, errorBody("unsupported_subscription", `不支持的订阅: ${domain}:${topic}`));
        return;
      }
      const data = (message.data ?? {}) as Record<string, unknown>;
      if (domain === "logs") {
        const replayRaw = data.replay;
        const replay = typeof replayRaw === "number" ? Math.min(Math.max(Math.floor(replayRaw), 0), 500) : 100;
        const entries = this.logReplay?.(replay) ?? [];
        this.sendResponse(connectionId, requestId, true, {});
        void this.sendEvent(connectionId, domain, "snapshot", { entries }, { topic });
        result.replay = () => undefined; // 快照已在上方按 replay 参数发出
        return;
      }
      if (domain === "maisaka_monitor") {
        this.sendResponse(connectionId, requestId, true, {});
        this.replayMaisakaMonitor(connectionId, topic, Number(data.since_event_id ?? 0), Number(data.replay_limit ?? 1000));
        return;
      }
      this.sendResponse(connectionId, requestId, true, {});
      this.enqueue(connection, {
        op: "event",
        domain,
        topic,
        event: "snapshot",
        data: { progress: Object.fromEntries(this.pluginProgress) },
      });
      return;
    }

    if (op === "unsubscribe") {
      const domain = String(message.domain ?? "");
      const topic = String(message.topic ?? "");
      if (!domain || !topic) {
        this.sendResponse(connectionId, requestId, false, undefined, errorBody("invalid_unsubscribe", "缺少 domain 或 topic"));
        return;
      }
      // 退订不存在的订阅不报错（附录 B.2 ③）
      this.unsubscribe(connection, domain, topic);
      this.sendResponse(connectionId, requestId, true, {});
      return;
    }

    if (op === "call") {
      const domain = String(message.domain ?? "");
      if (domain !== "chat") {
        this.sendResponse(connectionId, requestId, false, undefined, errorBody("unsupported_domain", `不支持的调用域: ${domain}`));
        return;
      }
      this.handleChatCall(connection, message);
      return;
    }

    this.sendResponse(connectionId, requestId, false, undefined, errorBody("unsupported_operation", `不支持的操作: ${op}`));
  }

  private replayMaisakaMonitor(connectionId: string, topic: string, sinceEventId: number, replayLimitRaw: number): void {
    const replayLimit = Math.min(Math.max(Number.isFinite(replayLimitRaw) ? Math.floor(replayLimitRaw) : 1000, 1), 10000);
    if (!this.db) {
      void this.sendEvent(connectionId, "maisaka_monitor", "stage.snapshot", { entries: 0, timestamp: nowFloatSeconds() }, { topic });
      return;
    }
    const rows = this.db.sqlite
      .prepare<[number, number], { event_id: number; event_type: string; session_id: string; timestamp: number; payload_json: string }>(
        "SELECT event_id, event_type, session_id, timestamp, payload_json FROM maisaka_monitor_events WHERE event_id > ? ORDER BY event_id ASC LIMIT ?",
      )
      .all(sinceEventId, replayLimit);
    for (const row of rows) {
      let payload: unknown = {};
      try {
        payload = JSON.parse(row.payload_json);
      } catch {
        payload = {};
      }
      void this.sendEvent(
        connectionId,
        "maisaka_monitor",
        row.event_type,
        { event_id: row.event_id, session_id: row.session_id, timestamp: row.timestamp, payload },
        { topic },
      );
    }
    void this.sendEvent(connectionId, "maisaka_monitor", "stage.snapshot", { entries: rows.length, timestamp: nowFloatSeconds() }, { topic });
  }

  // ------------------------------------------------------------------ chat 域

  private handleChatCall(connection: Connection, message: Record<string, unknown>): void {
    const requestId = typeof message.id === "string" ? message.id : null;
    const method = String(message.method ?? "").trim();
    const clientSessionId = String(message.session ?? "").trim();

    if (method === "session.open") {
      if (!clientSessionId) {
        this.sendResponse(connection.id, requestId, false, undefined, errorBody("missing_session", "聊天会话打开请求缺少 session"));
        return;
      }
      const sessionId = `${connection.id}:${clientSessionId}`;
      connection.chatSessions.set(clientSessionId, { sessionId, userName: "" });
      this.sendResponse(connection.id, requestId, true, {
        session: clientSessionId,
        session_id: sessionId,
        client_mode: "webui",
        client: { type: "webui" },
      });
      // 初始聊天状态由聊天引擎下发（D1 二期接入内核后补齐 send_initial_chat_state）
      return;
    }

    if (method === "session.close") {
      if (!clientSessionId) {
        this.sendResponse(connection.id, requestId, false, undefined, errorBody("missing_session", "聊天会话关闭请求缺少 session"));
        return;
      }
      if (!connection.chatSessions.has(clientSessionId)) {
        this.sendResponse(connection.id, requestId, false, undefined, errorBody("session_not_found", `找不到聊天会话: ${clientSessionId}`));
        return;
      }
      connection.chatSessions.delete(clientSessionId);
      this.sendResponse(connection.id, requestId, true, { session: clientSessionId });
      return;
    }

    if (method === "message.send") {
      if (!clientSessionId || !connection.chatSessions.has(clientSessionId)) {
        this.sendResponse(connection.id, requestId, false, undefined, errorBody("session_not_found", `找不到聊天会话: ${clientSessionId}`));
        return;
      }
      const data = (message.data ?? {}) as Record<string, unknown>;
      this.sendResponse(connection.id, requestId, true, { accepted: true, session: clientSessionId });
      // 处理引擎为 Python 内核（D1 二期接入）；当前接入点在此将 payload 转交
      this.logger.debug({ sessionId: connection.chatSessions.get(clientSessionId)?.sessionId }, "chat message.send（引擎未接入，已受理）");
      return;
    }

    if (method === "session.update_nickname") {
      if (!clientSessionId || !connection.chatSessions.has(clientSessionId)) {
        this.sendResponse(connection.id, requestId, false, undefined, errorBody("session_not_found", `找不到聊天会话: ${clientSessionId}`));
        return;
      }
      const data = (message.data ?? {}) as Record<string, unknown>;
      const userName = String(data.user_name ?? "");
      const entry = connection.chatSessions.get(clientSessionId)!;
      entry.userName = userName;
      this.sendResponse(connection.id, requestId, true, { session: clientSessionId, user_name: userName });
      return;
    }

    this.sendResponse(connection.id, requestId, false, undefined, errorBody("unsupported_method", `不支持的聊天方法: ${method}`));
  }
}

export function generateConnectionId(): string {
  return randomUUID().replaceAll("-", "");
}
