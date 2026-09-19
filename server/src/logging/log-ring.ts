/**
 * 日志环形缓冲（logs:main 订阅的快照回放源 + 实时广播源）。
 *
 * pino 经 multistream 把每行 JSON 同时写入本缓冲；WS 网关订阅建立时
 * 回放最近 limit 条（上限 500，对应 Python load_recent_logs），
 * 新日志到达时经 listener 实时广播。
 */

import { Writable } from "node:stream";

export class LogRingBuffer {
  private entries: unknown[] = [];
  private listener: ((entry: unknown) => void) | null = null;

  constructor(private readonly limit = 500) {}

  setListener(listener: ((entry: unknown) => void) | null): void {
    this.listener = listener;
  }

  push(entry: unknown): void {
    this.entries.push(entry);
    if (this.entries.length > this.limit) {
      this.entries.splice(0, this.entries.length - this.limit);
    }
    if (this.listener) {
      try {
        this.listener(entry);
      } catch {
        // 广播失败不影响日志主链路
      }
    }
  }

  recent(limit: number): unknown[] {
    const normalized = Math.max(1, Math.min(limit, 500));
    return this.entries.slice(-normalized);
  }

  /** pino multistream 目标流：逐行解析 JSON 后入缓冲。 */
  asStream(): Writable {
    const self = this;
    return new Writable({
      write(chunk, _encoding, callback) {
        try {
          const line = String(chunk).trim();
          if (line !== "") {
            try {
              self.push(JSON.parse(line));
            } catch {
              // 非 JSON 行（理论上 pino 只产 JSON）忽略
            }
          }
        } finally {
          callback();
        }
      },
    });
  }
}
