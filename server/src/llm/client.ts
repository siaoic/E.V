/**
 * LLM 客户端：读 model_config.toml → OpenAI 兼容调用。
 *
 * 对照 src/llm_models/：Python 侧用 openai SDK + 自定义 provider 配置。
 * TS 侧直接用 openai npm 包，从 api_providers + models + model_task_config
 * 中解析出第一个可用的 chat 模型（MVP：不用完整的 task 解析级联）。
 */

import { readFileSync, existsSync } from "node:fs";
import path from "node:path";
import { parse as parseToml } from "smol-toml";
import OpenAI from "openai";

export interface ModelConfig {
  apiKey: string;
  baseUrl: string;
  model: string;
  temperature: number;
  maxTokens: number;
}

export function readLlmConfig(rootDir: string): ModelConfig | null {
  const filePath = path.join(rootDir, "config", "model_config.toml");
  if (!existsSync(filePath)) {
    return null;
  }
  try {
    const data = parseToml(readFileSync(filePath, "utf8")) as Record<string, unknown>;

    // 取第一个 api_provider
    const providers = (data.api_providers as Array<Record<string, unknown>>) ?? [];
    if (providers.length === 0) return null;
    const provider = providers[0];
    const baseUrl = String(provider.base_url ?? "").replace(/\/+$/, "");
    const apiKey = String(provider.api_key ?? "");

    // 取第一个 model
    const models = (data.models as Array<Record<string, unknown>>) ?? [];
    if (models.length === 0) return null;
    const model = models[0];
    const modelId = String(model.model_name ?? "gpt-4o-mini");

    // 取 task_config 的 temperature 和 max_tokens（如果有）
    const taskConfig = (data.model_task_config as Record<string, Record<string, unknown>>) ?? {};
    const planner = taskConfig.planner ?? {};
    const temperature = typeof planner.temperature === "number" ? planner.temperature : 0.7;
    const maxTokens = typeof planner.max_tokens === "number" ? planner.max_tokens : 2048;

    return {
      apiKey,
      baseUrl: baseUrl || "https://api.openai.com/v1",
      model: modelId,
      temperature,
      maxTokens,
    };
  } catch {
    return null;
  }
}

export function createLlmClient(config: ModelConfig): OpenAI {
  return new OpenAI({
    apiKey: config.apiKey,
    baseURL: config.baseUrl,
    timeout: 60_000,
    maxRetries: 1,
  });
}

export async function chatCompletion(
  client: OpenAI,
  config: ModelConfig,
  messages: Array<{ role: "system" | "user" | "assistant"; content: string }>,
): Promise<{ text: string; promptTokens: number; completionTokens: number }> {
  const completion = await client.chat.completions.create({
    model: config.model,
    messages,
    temperature: config.temperature,
    max_tokens: config.maxTokens,
  });

  const choice = completion.choices[0];
  return {
    text: choice?.message?.content ?? "",
    promptTokens: completion.usage?.prompt_tokens ?? 0,
    completionTokens: completion.usage?.completion_tokens ?? 0,
  };
}
