/**
 * Host ↔ Runner RPC 信封与编解码（对照 src/plugin_runtime/protocol/{envelope,codec}.py）。
 *
 * 字节级契约（迁移风险 R3，黄金样例在 test/fixtures/plugin-protocol/）：
 * - 字段与默认值：protocol_version="1.0.0" / request_id 单调 int64 /
 *   message_type: "request"|"response"|"broadcast" / method / plugin_id /
 *   timestamp_ms / timeout_ms=30000 / payload: dict / error?: {code,message,details}；
 * - 序列化：MsgPack，ext 42=datetime(isoformat)、ext 43=date(isoformat)、
 *   Python Enum → 字符串值（Envelope 的 message_type 已是 str 枚举）；
 * - pydantic model_dump() 的键序 = 字段定义序，TS 侧构造对象必须保持同一键序
 *   才能字节级一致。
 */

import { ExtensionCodec, decode, encode } from "@msgpack/msgpack";

export const PROTOCOL_VERSION = "1.0.0";
export const MIN_SDK_VERSION = "1.0.0";
export const MAX_SDK_VERSION = "2.99.99";
export const DEFAULT_TIMEOUT_MS = 30_000;

const DATETIME_EXT_CODE = 42;
const DATE_EXT_CODE = 43;

export type MessageType = "request" | "response" | "broadcast";

export interface EnvelopeError {
  code: string;
  message: string;
  details?: Record<string, unknown>;
}

export interface Envelope {
  protocol_version: string;
  request_id: number;
  message_type: MessageType;
  method: string;
  plugin_id: string;
  timestamp_ms: number;
  timeout_ms: number;
  payload: Record<string, unknown>;
  error?: Record<string, unknown> | null;
}

/** 构造与 pydantic model_dump() 同键序的信封明文（字节级对齐的前提）。 */
export function buildEnvelopeObject(input: Partial<Envelope> & { request_id: number; message_type: MessageType }): Envelope {
  return {
    protocol_version: input.protocol_version ?? PROTOCOL_VERSION,
    request_id: input.request_id,
    message_type: input.message_type,
    method: input.method ?? "",
    plugin_id: input.plugin_id ?? "",
    timestamp_ms: input.timestamp_ms ?? Math.floor(Date.now()),
    timeout_ms: input.timeout_ms ?? DEFAULT_TIMEOUT_MS,
    payload: input.payload ?? {},
    error: input.error ?? null,
  };
}

function padIso(value: string): string {
  // Python datetime.isoformat() 微秒恰 6 位（含尾零）；Date 只有毫秒，需补齐
  const match = /^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})\.(\d{1,3})(.*)$/.exec(value);
  if (match) {
    return `${match[1]}.${match[2]!.padEnd(6, "0")}${match[3] ?? ""}`;
  }
  const noFrac = /^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})$/.exec(value);
  return noFrac ? `${noFrac[1]}.000000` : value;
}

const extensionCodec = new ExtensionCodec();

extensionCodec.register({
  type: DATETIME_EXT_CODE,
  encode: (value: unknown): Uint8Array | null => {
    if (value instanceof Date) {
      return new TextEncoder().encode(padIso(value.toISOString().replace("Z", "").split(".")[0] === value.toISOString().replace("Z", "") ? value.toISOString().replace("Z", "") : toIsoLocal(value)));
    }
    return null;
  },
  decode: (data: Uint8Array): unknown => {
    return new TextDecoder().decode(data); // 保留 ISO 字符串形态，由调用方按需转 Date
  },
});

extensionCodec.register({
  type: DATE_EXT_CODE,
  encode: (value: unknown): Uint8Array | null => {
    if (value instanceof Date && isDateOnly(value)) {
      return new TextEncoder().encode(value.toISOString().slice(0, 10));
    }
    return null;
  },
  decode: (data: Uint8Array): unknown => {
    return new TextDecoder().decode(data);
  },
});

function isDateOnly(_value: Date): boolean {
  return false; // TS 侧不区分 date/datetime，统一走 datetime 编码；仅影响解码来源标注
}

function toIsoLocal(date: Date): string {
  const pad = (v: number, w = 2) => String(v).padStart(w, "0");
  const ms = date.getMilliseconds();
  return (
    `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}` +
    `T${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}` +
    (ms > 0 ? `.${pad(ms, 3)}000` : "")
  );
}

/** Python msgpack.packb(obj, use_bin_type=True) 的 TS 等价编码。 */
export function packMsgPack(value: unknown): Uint8Array {
  return encode(value, { extensionCodec, ignoreUndefined: true });
}

export function unpackMsgPack<T = Record<string, unknown>>(data: Uint8Array): T {
  return decode(data, { extensionCodec }) as T;
}

/** 信封 → MsgPack 字节（键序与 pydantic model_dump 一致）。 */
export function encodeEnvelope(envelope: Envelope): Uint8Array {
  return packMsgPack({
    protocol_version: envelope.protocol_version,
    request_id: envelope.request_id,
    message_type: envelope.message_type,
    method: envelope.method,
    plugin_id: envelope.plugin_id,
    timestamp_ms: envelope.timestamp_ms,
    timeout_ms: envelope.timeout_ms,
    payload: envelope.payload,
    error: envelope.error ?? null,
  });
}

/** MsgPack 字节 → 信封；缺字段按默认值补齐（对应 Envelope.model_validate）。 */
export function decodeEnvelope(data: Uint8Array): Envelope {
  const raw = unpackMsgPack<Record<string, unknown>>(data);
  const messageType = String(raw.message_type ?? "");
  if (!["request", "response", "broadcast"].includes(messageType)) {
    throw new Error(`非法 message_type: ${messageType}`);
  }
  return {
    protocol_version: String(raw.protocol_version ?? PROTOCOL_VERSION),
    request_id: Number(raw.request_id ?? 0),
    message_type: messageType as MessageType,
    method: String(raw.method ?? ""),
    plugin_id: String(raw.plugin_id ?? ""),
    timestamp_ms: Number(raw.timestamp_ms ?? 0),
    timeout_ms: Number(raw.timeout_ms ?? DEFAULT_TIMEOUT_MS),
    payload: (raw.payload as Record<string, unknown> | undefined) ?? {},
    error: (raw.error as Record<string, unknown> | null | undefined) ?? null,
  };
}

/** 基于当前请求构造响应信封（对应 Envelope.make_response）。 */
export function makeResponse(
  request: Envelope,
  payload?: Record<string, unknown>,
  error?: { code: string; message: string; details?: Record<string, unknown> } | null,
): Envelope {
  return {
    protocol_version: request.protocol_version,
    request_id: request.request_id,
    message_type: "response",
    method: request.method,
    plugin_id: request.plugin_id,
    timestamp_ms: Date.now(),
    timeout_ms: request.timeout_ms,
    payload: payload ?? {},
    error: error ?? null,
  };
}
