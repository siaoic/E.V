/**
 * Chat Completions against llama-server. Thinking is the template's `enable_thinking` switch
 * sent per request through `chat_template_kwargs`; templates without that variable ignore it.
 * The server returns the chain of thought as `reasoning_content` (`--reasoning-format deepseek`),
 * which the shared Chat assembly already maps; past thinking is not replayed.
 */
import type { NativeChatMessage } from '../transport/native-types.ts';
import type { ModelSpec, ToolSchema, Logger } from '../../core/types.ts';
import { nullLogger } from '../../core/util.ts';
import { OpenAIHttpClient } from '../transport/chat.ts';
import { mapTools, renderMessagesWithMedia, type CompatMediaOptions } from '../transport/history.ts';

export function buildLlamaCppRequestBody(
  spec: ModelSpec,
  messages: NativeChatMessage[],
  tools?: ToolSchema[],
  media?: CompatMediaOptions,
): Record<string, unknown> {
  const body: Record<string, unknown> = {
    model: spec.model,
    messages: renderMessagesWithMedia(messages, media),
    chat_template_kwargs: { enable_thinking: spec.thinking },
  };
  if (spec.thinking && spec.reasoningEffort) body.reasoning_effort = spec.reasoningEffort;
  if (spec.temperature !== undefined) body.temperature = spec.temperature;
  if (spec.maxTokens !== undefined) body.max_tokens = spec.maxTokens;
  const mapped = mapTools(tools);
  if (mapped) body.tools = mapped;
  return body;
}

export class LlamaCppProvider extends OpenAIHttpClient {
  private readonly apiKey?: string;
  private readonly media?: CompatMediaOptions;

  constructor(opts: { baseUrl: string; apiKey?: string; log?: Logger; media?: CompatMediaOptions }) {
    super(opts.baseUrl, opts.log ?? nullLogger());
    this.apiKey = opts.apiKey;
    this.media = opts.media;
  }

  protected buildBody(spec: ModelSpec, messages: NativeChatMessage[], tools?: ToolSchema[]): Record<string, unknown> {
    return buildLlamaCppRequestBody(spec, messages, tools, this.media);
  }

  protected headers(): Record<string, string> {
    const headers: Record<string, string> = { 'Content-Type': 'application/json' };
    if (this.apiKey) headers.Authorization = `Bearer ${this.apiKey}`;
    return headers;
  }
}
