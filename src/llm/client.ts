export type ToolCall = { id: string; type: 'function'; function: { name: string; arguments: string } };

// 多模态内容：user 消息可以是纯文本，或文本+图片混合的 OpenAI content parts
// （GLM-4V 与 llama.cpp 的 chat 端点都认这种格式；image 即 data URL 或 http(s) 地址）
export type MessagePart =
  | { type: 'text'; text: string }
  | { type: 'image_url'; image_url: { url: string } };

export type MessageContent = string | MessagePart[];

export type ChatMessage =
  | { role: 'system' | 'user'; content: MessageContent }
  | { role: 'assistant'; content: string | null; tool_calls?: ToolCall[] }
  | { role: 'tool'; content: string; tool_call_id: string };

export type ToolDefinition = {
  type: 'function';
  function: { name: string; description: string; parameters: Record<string, unknown> };
};

export type AssistantReply = { content: string | null; toolCalls: ToolCall[] };

/** 端点限流（429）导致的失败；外层窗口据此降温，别继续打接口把配额焊死。 */
export type RateLimitedError = Error & { rateLimited: true };

export function isRateLimited(error: unknown): error is RateLimitedError {
  return typeof error === 'object' && error !== null && (error as { rateLimited?: boolean }).rateLimited === true;
}

export type LlmPort = {
  complete(messages: ChatMessage[], tools?: ToolDefinition[], options?: { timeoutMs?: number }): Promise<AssistantReply>;
  /** 视图能力：给一张图（data URL 或 http 地址）返回一句描述，走主模型（现用模型自带视觉） */
  describeImage?(input: { image: string; prompt?: string }): Promise<string>;
};

export type LlmClientOptions = {
  baseUrl: string;
  apiKey: string;
  model: string;
  extraBody?: Record<string, unknown>;
  fetch?: typeof globalThis.fetch;
};

// 单次请求超时；窗口挂死会让整条发言链停摆，必须有上限。
// 超时中止按可重试错误处理：模型延迟波动大（实测会从 3s 涨到 10s），掐断后退避重试等低延迟窗口
const REQUEST_TIMEOUT_MS = 7000;
const RETRY_DELAYS_MS = [1000, 2000, 4000];
// 重试次数上限（含首次）。任何可重试结果都必须过这一关：429 曾走裸 `continue`、没有次数判断 ——
// 账户被限流（GLM 1302）时这个循环永不返回，窗口的 Promise 永远不落地，windowRunning 一直为 true，
// 于是弹幕、心跳、播报开始全都开不了窗，直播可以无限期不说话。
const MAX_ATTEMPTS = RETRY_DELAYS_MS.length + 1;
// 一次调用（含退避与重试）的总预算。单次 7s 超时 × 4 次尝试 ≈ 35s，而窗口是链条上唯一的节拍器：
// 挂住 35 秒就是 35 秒不说话，外层空窗口还会连着重试 3 次，最坏静默是这个预算乘出来的。
// 预算到点即放弃本次调用，让窗口尽快收场，把机会交还给下一个事件（弹幕/心跳）。
const TOTAL_BUDGET_MS = 15_000;
// 退避后至少还要留出一次请求的时间，才值得再试一次
const MIN_RETRY_BUDGET_MS = 1000;

export function createLlmClient({
  baseUrl,
  apiKey,
  model,
  extraBody,
  fetch = globalThis.fetch,
}: LlmClientOptions) {
  // 端点地址末尾的斜杠会让 `${base}/chat/completions` 变成 `//chat/completions`，统一去掉
  const rootUrl = baseUrl.replace(/\/+$/, '');
  // extraBody 是端点专属开关（如智谱的 thinking）。端点不认时（400 里点名了字段）去掉重发一次并记住，
  // 否则每个窗口都白撞一次 400，整条发言链断在第一次请求上。
  let extraBodyDropped = false;
  const bodyFor = (fields: Record<string, unknown>): string =>
    JSON.stringify(extraBodyDropped || !extraBody ? fields : { ...fields, ...extraBody });
  const rejectedExtraBody = (error: unknown): boolean => {
    if (!extraBody || extraBodyDropped) return false;
    const message = error instanceof Error ? error.message : String(error);
    if (!message.includes(' 400 ')) return false;
    return Object.keys(extraBody).some((key) => message.includes(key));
  };
  const send = (base: string, body: string, timeoutMs = REQUEST_TIMEOUT_MS) => {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    return fetch(`${base}/chat/completions`, {
      method: 'POST',
      headers: {
        'content-type': 'application/json',
        Authorization: `Bearer ${apiKey}`,
      },
      body,
      signal: controller.signal,
    }).finally(() => clearTimeout(timer));
  };

  // 超时/限流退避重试的公共请求循环。次数（MAX_ATTEMPTS）与总预算（deadline）双重封顶，
  // 保证它一定会返回或抛错：只要它可能不返回，整条发言链就可能永久停摆。
  const requestWithRetry = async (base: string, body: string, timeoutMs = REQUEST_TIMEOUT_MS): Promise<AssistantReply> => {
    const backoffTotalMs = RETRY_DELAYS_MS.reduce((sum, delay) => sum + delay, 0);
    // 调用方给了更长的单次超时（dream 的 20s 例外）时，预算至少要容得下一次尝试加完整退避
    const deadline = Date.now() + Math.max(TOTAL_BUDGET_MS, timeoutMs + backoffTotalMs);
    let lastError: unknown;
    let rateLimited = false;
    for (let attempt = 0; attempt < MAX_ATTEMPTS; attempt += 1) {
      if (attempt > 0) {
        const delay = RETRY_DELAYS_MS[Math.min(attempt - 1, RETRY_DELAYS_MS.length - 1)];
        if (deadline - Date.now() < delay + MIN_RETRY_BUDGET_MS) break;
        await new Promise((resolve) => setTimeout(resolve, delay));
      }
      try {
        const response = await send(base, body, Math.min(timeoutMs, Math.max(1, deadline - Date.now())));
        if (response.status === 429) {
          rateLimited = true;
          lastError = new Error(`LLM request failed: 429 ${await response.text()}`);
          continue;
        }
        if (!response.ok) {
          const error = new Error(`LLM request failed: ${response.status} ${await response.text()}`);
          (error as Error & { permanent?: boolean }).permanent = true;
          throw error;
        }
        const data = (await response.json()) as {
          choices: { message: { content: string | null; tool_calls?: ToolCall[] } }[];
        };
        const message = data.choices[0].message;
        return { content: message.content, toolCalls: message.tool_calls ?? [] };
      } catch (error) {
        // 永久错误（非 429 的非 2xx）不重试；可重试错误记下来，由循环头决定还能不能再试
        if ((error as Error & { permanent?: boolean }).permanent) throw error;
        lastError = error;
      }
    }
    if (lastError instanceof Error) {
      // 中途撞过限流就把标记带上：即使最后一次是超时，外层也该降温
      if (rateLimited) (lastError as Error & { rateLimited?: boolean }).rateLimited = true;
      throw lastError;
    }
    throw new Error('LLM request failed: 重试次数与预算都已用尽');
  };

  return {
    async complete(messages: ChatMessage[], tools?: ToolDefinition[], options?: { timeoutMs?: number }): Promise<AssistantReply> {
      const call = () => requestWithRetry(rootUrl, bodyFor({ model, messages, tools }), options?.timeoutMs);
      try {
        return await call();
      } catch (error) {
        if (!rejectedExtraBody(error)) throw error;
        extraBodyDropped = true;
        console.warn(`[llm] 端点不接受 extraBody，已去掉重发（${Object.keys(extraBody ?? {}).join(', ')}）`);
        return call();
      }
    },

    async describeImage(input: { image: string; prompt?: string }): Promise<string> {
      const call = () =>
        requestWithRetry(
          rootUrl,
          bodyFor({
            model,
            messages: [
              {
                role: 'user',
                content: [
                  { type: 'text', text: input.prompt ?? '请用中文详细描述这张图片的内容。如果有文字，请把文字描述概括出来，请留意其主题、直观感受，输出为一段平文本，最多100字，请注意不要分点，就输出一段文本' },
                  { type: 'image_url', image_url: { url: input.image } },
                ],
              },
            ],
          }),
        );
      let reply: AssistantReply;
      try {
        reply = await call();
      } catch (error) {
        if (!rejectedExtraBody(error)) throw error;
        extraBodyDropped = true;
        reply = await call();
      }
      return (reply.content ?? '').trim();
    },
  };
}
