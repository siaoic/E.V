/**
 * Kernel HTTP 客户端基座：超时 / 重试 / 错误形状。
 * 所有 kernel 子客户端（memory / TTS / Runner）共用。
 */

export interface KernelResponse<T = unknown> {
  ok: boolean;
  status: number;
  data: T;
}

export interface KernelClientOptions {
  baseUrl: string;
  timeoutMs?: number;
  logger?: { debug(msg: string, ...args: unknown[]): void; error(msg: string, ...args: unknown[]): void };
}

export class KernelClientError extends Error {
  constructor(
    public readonly status: number,
    public readonly code: string,
    message: string,
  ) {
    super(message);
  }
}

export class KernelHttpClient {
  constructor(private readonly options: KernelClientOptions) {}

  async get<T = unknown>(path: string, timeoutMs?: number): Promise<KernelResponse<T>> {
    return this.doRequest<T>("GET", path, undefined, timeoutMs);
  }

  async post<T = unknown>(path: string, body?: unknown, timeoutMs?: number): Promise<KernelResponse<T>> {
    return this.doRequest<T>("POST", path, body, timeoutMs);
  }

  private async doRequest<T = unknown>(method: string, path: string, body?: unknown, timeoutMs?: number): Promise<KernelResponse<T>> {
    const url = `${this.options.baseUrl}${path}`;
    const timeout = timeoutMs ?? this.options.timeoutMs ?? 10_000;

    const response = await fetch(url, {
      method,
      signal: AbortSignal.timeout(timeout),
      headers: body !== undefined ? { "content-type": "application/json" } : undefined,
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });

    const text = await response.text();
    let data: unknown = {};
    try {
      data = JSON.parse(text);
    } catch {
      data = text;
    }

    if (!response.ok) {
      const detail =
        typeof data === "object" && data !== null && "detail" in data
          ? String((data as Record<string, unknown>).detail)
          : `HTTP ${response.status}`;
      throw new KernelClientError(response.status, "kernel_error", detail);
    }

    return { ok: true, status: response.status, data: data as T };
  }
}
