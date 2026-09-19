/**
 * Chat 引擎桥接客户端：TS WS 网关 → Python chat_bridge_server 的 HTTP 转发。
 *
 * `message.send` 的处理引擎在 Python 内核（D1 二期）；本客户端把 WS 收到的
 * 聊天调用转发到内核 HTTP 端点，使 WS 网关的 chat 域从「受理并记录」变为
 * 真实处理。桥接服务未启动时调用方收到 `bridge_unavailable`，不静默。
 */

import { KernelHttpClient } from "./http-client.js";

export interface ChatBridgeClient {
  /** 打开会话（对应 chat_manager.connect）。 */
  connect(sessionId: string, clientSessionId: string, connectionId: string, userId?: string, userName?: string): Promise<void>;
  /** 发消息（走完整 maisaka 管线）。 */
  sendMessage(sessionId: string, payload: Record<string, unknown>): Promise<void>;
  /** 关闭会话。 */
  disconnect(sessionId: string): Promise<void>;
  /** 更新昵称。 */
  updateNickname(sessionId: string, userName: string): Promise<{ userName: string }>;
}

export class HttpChatBridge implements ChatBridgeClient {
  private client: KernelHttpClient;

  constructor(baseUrl: string, logger?: { debug(msg: string, ...args: unknown[]): void; error(msg: string, ...args: unknown[]): void }) {
    this.client = new KernelHttpClient({ baseUrl, timeoutMs: 30_000, logger });
  }

  async connect(sessionId: string, clientSessionId: string, connectionId: string, userId = "", userName = "人类"): Promise<void> {
    await this.client.post("/connect", {
      session_id: sessionId,
      client_session_id: clientSessionId,
      connection_id: connectionId,
      user_id: userId,
      user_name: userName,
      platform: "webui",
    });
  }

  async sendMessage(sessionId: string, payload: Record<string, unknown>): Promise<void> {
    await this.client.post("/send", { session_id: sessionId, payload });
  }

  async disconnect(sessionId: string): Promise<void> {
    await this.client.post("/disconnect", { session_id: sessionId });
  }

  async updateNickname(sessionId: string, userName: string): Promise<{ userName: string }> {
    const res = await this.client.post<{ success: boolean; user_name: string }>("/update_nickname", {
      session_id: sessionId,
      user_name: userName,
    });
    return { userName: res.data.user_name };
  }
}
