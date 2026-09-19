/**
 * 最小可行消息管线（MVP）：消息入库 → LLM → 回复入库。
 *
 * D1 二期第一块：让 WS `message.send` 真正产生 AI 回复。
 * MVP 不读历史（raw_content 是 msgpack 编码），后续逐步补齐
 * maisaka 完整特性（注意力 / 表达学习 / 世界事件 / 频率控制 / 记忆 / …）。
 */

import type OpenAI from "openai";

import type { DbHandle } from "../db/client.js";
import { maiMessages } from "../db/schema.js";
import { sqliteDatetime } from "../db/datetime.js";
import { chatCompletion, type ModelConfig } from "../llm/client.js";

export interface PipelineMessage {
  content: string;
  userId: string;
  userName: string;
  sessionId: string;
  images?: string[];
}

export interface PipelineResult {
  reply: string;
  promptTokens: number;
  completionTokens: number;
  durationMs: number;
}

const DEFAULT_SYSTEM_PROMPT = `你是麦麦，一个直播间 AI 助手。你的回复要自然、口语化、有个性。
不要用 markdown 格式，直接输出纯文本回复。`;

export async function processMessage(
  db: DbHandle,
  client: OpenAI,
  config: ModelConfig,
  message: PipelineMessage,
): Promise<PipelineResult> {
  const startedAt = Date.now();
  const now = new Date();

  // 1. 用户消息入库
  const messageId = `ts-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`;
  db.drizzle.insert(maiMessages).values({
    messageId,
    timestamp: sqliteDatetime(now),
    platform: "webui",
    userId: message.userId,
    userNickname: message.userName,
    sessionId: message.sessionId,
    rawContent: Buffer.from(JSON.stringify({ content: message.content })),
    isMentioned: false, isAt: false, isEmoji: false,
    isPicture: false, isCommand: false, isNotify: false,
  }).run();

  // 2. 调 LLM（MVP：system prompt + 当前消息；历史回补待 raw_content 解码）
  const llmMessages = [
    { role: "system" as const, content: DEFAULT_SYSTEM_PROMPT },
    { role: "user" as const, content: message.content },
  ];
  const result = await chatCompletion(client, config, llmMessages);

  // 3. 回复入库
  const replyId = `ts-reply-${Date.now()}`;
  db.drizzle.insert(maiMessages).values({
    messageId: replyId,
    timestamp: sqliteDatetime(new Date()),
    platform: "webui",
    userId: "bot",
    userNickname: "麦麦",
    sessionId: message.sessionId,
    replyTo: messageId,
    rawContent: Buffer.from(JSON.stringify({ content: result.text })),
    isMentioned: false, isAt: false, isEmoji: false,
    isPicture: false, isCommand: false, isNotify: false,
  }).run();

  return {
    reply: result.text,
    promptTokens: result.promptTokens,
    completionTokens: result.completionTokens,
    durationMs: Date.now() - startedAt,
  };
}
