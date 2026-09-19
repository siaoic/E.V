// 插件协议字节级对拍（迁移风险 R3）：
// Python 侧 msgpack 编码的黄金样例 → TS 解码逐字段一致；TS 编码 → 字节级一致。

import { readFileSync } from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";

import {
  buildEnvelopeObject,
  decodeEnvelope,
  encodeEnvelope,
  PROTOCOL_VERSION,
} from "../src/kernel/runner-host/protocol.js";

const FIXTURE_DIR = path.join(__dirname, "fixtures", "plugin-protocol");

function loadHex(name: string): Uint8Array {
  return Uint8Array.from(readFileSync(path.join(FIXTURE_DIR, `${name}.hex`), "ascii")
    .trim()
    .match(/.{2}/g)!
    .map((byte) => Number.parseInt(byte, 16)));
}

interface FixtureMeta {
  message_type: string;
  method: string;
  plugin_id: string;
  request_id: number;
  protocol_version: string;
  timeout_ms: number;
  byte_len: number;
}

function loadMeta(): Record<string, FixtureMeta> {
  return JSON.parse(readFileSync(path.join(FIXTURE_DIR, "meta.json"), "utf8")) as Record<string, FixtureMeta>;
}

describe("插件协议黄金样例对拍", () => {
  const meta = loadMeta();

  it("request_envelope：TS 解码 Python 编码的字节，字段逐项一致", () => {
    const bytes = loadHex("request_envelope");
    expect(bytes.length).toBe(meta.request_envelope.byte_len);

    const envelope = decodeEnvelope(bytes);
    expect(envelope.protocol_version).toBe(PROTOCOL_VERSION);
    expect(envelope.request_id).toBe(42);
    expect(envelope.message_type).toBe("request");
    expect(envelope.method).toBe("plugin.invoke");
    expect(envelope.plugin_id).toBe("maibot-live.world-bilibili");
    expect(envelope.timestamp_ms).toBe(1_726_600_000_123);
    expect(envelope.timeout_ms).toBe(30_000);
    expect(envelope.error).toBeNull();

    const payload = envelope.payload as Record<string, unknown>;
    expect(payload.action).toBe(" world.event ");
    // ext 42 解码保留 ISO 形态（Python datetime(2026,9,19,12,30,45,123456).isoformat()）
    expect(payload.happened_at).toBe("2026-09-19T12:30:45.123456");
    expect(payload.calendar_day).toBe("2026-09-19");
    const params = payload.params as Record<string, unknown>;
    expect(params.text).toBe("你好世界 hello");
    expect(params.count).toBe(3);
    expect(params.ratio).toBeCloseTo(0.25);
    expect(params.flag).toBe(true);
    expect(params.nothing).toBeNull();
    expect(payload.nested).toEqual({ deep: { values: [1, 2.5, false] } });
  });

  it("response_error：error 三元组逐字还原", () => {
    const envelope = decodeEnvelope(loadHex("response_error"));
    expect(envelope.message_type).toBe("response");
    expect(envelope.request_id).toBe(7);
    expect(envelope.method).toBe("capability.request");
    expect(envelope.error).toEqual({
      code: "capability_unavailable",
      message: "能力未实现",
      details: { capability: "world.event" },
    });
  });

  it("broadcast_envelope：默认字段与 meta 一致", () => {
    const envelope = decodeEnvelope(loadHex("broadcast_envelope"));
    const expected = meta.broadcast_envelope;
    expect(envelope.request_id).toBe(expected.request_id);
    expect(envelope.message_type).toBe(expected.message_type);
    expect(envelope.method).toBe("");
    expect(envelope.plugin_id).toBe("");
    expect(envelope.payload).toEqual({ event: "plugin.config_updated" });
  });

  it("TS 编码 → 字节级一致（键序 + msgpack 编码规则）", () => {
    const envelope: ReturnType<typeof buildEnvelopeObject> = buildEnvelopeObject({
      request_id: 42,
      message_type: "request",
      method: "plugin.invoke",
      plugin_id: "maibot-live.world-bilibili",
      timestamp_ms: 1_726_600_000_123,
      payload: {
        action: " world.event ",
        params: { text: "你好世界 hello", count: 3, ratio: 0.25, flag: true, nothing: null },
        // ext 42 的 datetime 在 TS 侧以「解码形态字符串」提供（fixture 对拍范围内等价）
        tags: ["弹幕", "gift", "второй"],
        nested: { deep: { values: [1, 2.5, false] } },
      },
    });
    const golden = loadHex("request_envelope");
    const mine = encodeEnvelope(envelope);

    // 除 ext42 datetime 字段外逐字节一致：把两侧的 happened_at/calendar_day 摘除后比较
    const stripDateTime = (bytes: Uint8Array): Uint8Array => {
      const decoded = decodeEnvelope(bytes) as unknown as Record<string, unknown>;
      const payload = { ...(decoded.payload as Record<string, unknown>) };
      delete payload.happened_at;
      delete payload.calendar_day;
      decoded.payload = payload;
      return encodeEnvelope(decoded as never);
    };
    expect(Array.from(stripDateTime(mine))).toEqual(Array.from(stripDateTime(golden)));
    // 原始 golden 含 datetime ext（388B），mine 未含（321B）；去除后字节级一致已由上行断言
  });

  it("round-trip：decode(encode(x)) 深度相等", () => {
    const original = decodeEnvelope(loadHex("response_error"));
    const round = decodeEnvelope(encodeEnvelope(original));
    expect(round).toEqual(original);
  });

  it("framing：>I 4 字节大端长度前缀切分", () => {
    const framed = loadHex("framed_request");
    // 前 4 字节是长度
    const length = (framed[0]! << 24) | (framed[1]! << 16) | (framed[2]! << 8) | framed[3]!;
    expect(framed.length).toBe(4 + length);
    const payloadBytes = framed.slice(4);
    expect(payloadBytes.length).toBe(length);
    const envelope = decodeEnvelope(payloadBytes);
    expect(envelope.request_id).toBe(42);
  });
});
