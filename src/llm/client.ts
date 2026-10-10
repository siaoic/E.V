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

export type LlmPort = {
  complete(messages: ChatMessage[], tools?: ToolDefinition[], options?: { timeoutMs?: number }): Promise<AssistantReply>;
  /** 视图能力：给一张图（data URL 或 http 地址）返回一句描述；未配视觉模型时用默认模型 */
  describeImage?(input: { image: string; prompt?: string }): Promise<string>;
};

export type LlmClientOptions = {
  baseUrl: string;
  apiKey: string;
  model: string;
  /** 视觉模型（如 glm-4v-flash）；不配则视图调用回落到 model */
  visionModel?: string;
  /** 视觉请求单独的端点（如本地 llama.cpp 带 mmproj）；不配则用 baseUrl */
  visionBaseUrl?: string;
  extraBody?: Record<string, unknown>;
  fetch?: typeof globalThis.fetch;
};

const RATE_LIMIT_BACKOFF_MS = [1000, 2000, 4000];
// 单次请求超时；窗口挂死会让整条发言链停摆，必须有上限。
// 超时中止按可重试错误处理：模型延迟波动大（实测会从 3s 涨到 10s），掐断后退避重试等低延迟窗口
const REQUEST_TIMEOUT_MS = 7000;
const RETRY_DELAYS_MS = [1000, 2000, 4000];

export function createLlmClient({
  baseUrl,
  apiKey,
  model,
  visionModel,
  visionBaseUrl,
  extraBody,
  fetch = globalThis.fetch,
}: LlmClientOptions) {
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

  // 超时/限流退避重试的公共请求循环
  const requestWithRetry = async (base: string, body: string, timeoutMs?: number): Promise<AssistantReply> => {
    for (let attempt = 0; ; attempt += 1) {
      if (attempt > 0) {
        await new Promise((resolve) => setTimeout(resolve, RETRY_DELAYS_MS[Math.min(attempt - 1, RETRY_DELAYS_MS.length - 1)]));
      }
      try {
        const response = await send(base, body, timeoutMs);
        if (response.status === 429) continue;
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
        const retryable = !(error as Error & { permanent?: boolean }).permanent;
        if (!retryable || attempt >= RETRY_DELAYS_MS.length) throw error;
      }
    }
  };

  return {
    async complete(messages: ChatMessage[], tools?: ToolDefinition[], options?: { timeoutMs?: number }): Promise<AssistantReply> {
      return requestWithRetry(baseUrl, JSON.stringify({ model, messages, tools, ...extraBody }), options?.timeoutMs);
    },

    async describeImage(input: { image: string; prompt?: string }): Promise<string> {
      const reply = await requestWithRetry(
        visionBaseUrl ?? baseUrl,
        JSON.stringify({
          model: visionModel ?? model,
          messages: [
            {
              role: 'user',
              content: [
                { type: 'text', text: input.prompt ?? '请用中文详细描述这张图片的内容。如果有文字，请把文字描述概括出来，请留意其主题、直观感受，输出为一段平文本，最多100字，请注意不要分点，就输出一段文本' },
                { type: 'image_url', image_url: { url: input.image } },
              ],
            },
          ],
          ...extraBody,
        }),
      );
      return (reply.content ?? '').trim();
    },
  };
}
