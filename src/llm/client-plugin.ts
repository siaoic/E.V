import type { Context } from '@deepseek-ai/cordis';
import { createLlmClient, type LlmPort } from './client.ts';

export type LlmClientPluginConfig = {
  baseUrl: string;
  apiKey: string;
  model: string;
  /** 视觉模型（如 glm-4v-flash）；留空则视图调用回落到 model */
  visionModel?: string;
  /** 视觉请求单独端点（如本地 llama.cpp 带 mmproj）；留空用 baseUrl */
  visionBaseUrl?: string;
  /** 字符串形式的原始 JSON（env 替换后 parse），如 '{"thinking":{"type":"disabled"}}' */
  extraBody?: string;
};

declare module '@deepseek-ai/cordis' {
  interface Context {
    llm: LlmPort;
  }
}

export const apply = Object.assign(
  function llmClient(ctx: Context, config: LlmClientPluginConfig) {
    const missing = (['baseUrl', 'apiKey', 'model'] as const).filter((key) => !String(config[key] ?? '').trim());
    if (missing.length > 0) throw new Error(`缺少必填配置：${missing.join(', ')}`);
    let extraBody: Record<string, unknown> | undefined;
    if (typeof config.extraBody === 'string' && config.extraBody.trim()) extraBody = JSON.parse(config.extraBody);
    const visionModel = config.visionModel?.trim() || undefined;
    const visionBaseUrl = config.visionBaseUrl?.trim() || undefined;
    ctx.provide(
      'llm',
      createLlmClient({
        baseUrl: config.baseUrl,
        apiKey: config.apiKey,
        model: config.model,
        visionModel,
        visionBaseUrl,
        extraBody,
      }),
    );
  },
  { inject: [] as string[] },
);

export const config = {
  baseUrl: '${LLM_BASE_URL}',
  apiKey: '${LLM_API_KEY}',
  model: '${LLM_MODEL}',
  visionModel: '${VISION_MODEL:-}',
  visionBaseUrl: '${VISION_BASE_URL:-}',
  extraBody: '${LLM_EXTRA_BODY:-}',
};
