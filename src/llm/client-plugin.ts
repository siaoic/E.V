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

/** 解析 LLM_EXTRA_BODY；空串视为未配置。 */
export function parseExtraBody(raw: string | undefined): Record<string, unknown> | undefined {
  if (typeof raw !== 'string' || !raw.trim()) return undefined;
  return JSON.parse(raw) as Record<string, unknown>;
}

/**
 * 智谱端点在 thinking 打开时响应常超过 7 秒（实测不关时几乎每个窗口都撞超时），必须显式关掉。
 * run-live.bat 只在 base URL 含 bigmodel.cn 时补这份默认值；直接用 `node src/main.ts` 起进程就没有
 * 那层兜底，于是每个窗口都超时、连着几次空窗口后链条停摆。非智谱端点一律不塞（会回 400 unsupported_parameter，
 * client 侧有"去掉 extraBody 重发一次"的兜底，但仍不该主动塞）。
 */
export function defaultExtraBody(baseUrl: string): Record<string, unknown> | undefined {
  return /bigmodel\.cn/i.test(baseUrl) ? { thinking: { type: 'disabled' } } : undefined;
}

export const apply = Object.assign(
  function llmClient(ctx: Context, config: LlmClientPluginConfig) {
    const missing = (['baseUrl', 'apiKey', 'model'] as const).filter((key) => !String(config[key] ?? '').trim());
    if (missing.length > 0) throw new Error(`缺少必填配置：${missing.join(', ')}`);
    const configured = parseExtraBody(config.extraBody);
    const extraBody = configured ?? defaultExtraBody(config.baseUrl);
    if (!configured && extraBody) console.log('[llm] 未配置 LLM_EXTRA_BODY，按智谱端点默认关闭 thinking');
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
