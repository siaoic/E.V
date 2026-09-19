# -*- coding: utf-8 -*-
"""生成插件协议黄金字节样例（Python 侧 msgpack 编码 → TS 侧解码对拍）。

用法：在仓库根目录执行  python server/scripts/dump_protocol_fixtures.py

产物（server/test/fixtures/plugin-protocol/）：
- request_envelope.hex      完整请求信封（含 datetime ext 42 / CJK / 浮点 / 嵌套）
- response_error.hex        错误响应信封
- broadcast_envelope.hex    广播信封（全默认字段）
- framed_request.hex        >I 4 字节大端长度前缀 + request_envelope
- meta.json                 各样例的期望字段（TS 侧断言用）
"""

from __future__ import annotations

import json
import sys
from datetime import date, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from src.plugin_runtime.protocol.codec import MsgPackCodec  # noqa: E402
from src.plugin_runtime.protocol.envelope import Envelope, MessageType  # noqa: E402

OUT_DIR = Path(__file__).resolve().parent.parent / "test" / "fixtures" / "plugin-protocol"

PAYLOAD_DATETIME = datetime(2026, 9, 19, 12, 30, 45, 123456)


def build_request() -> Envelope:
    return Envelope(
        protocol_version="1.0.0",
        request_id=42,
        message_type=MessageType.REQUEST,
        method="plugin.invoke",
        plugin_id="maibot-live.world-bilibili",
        timestamp_ms=1_726_600_000_123,
        timeout_ms=30_000,
        payload={
            "action": " world.event ",
            "params": {"text": "你好世界 hello", "count": 3, "ratio": 0.25, "flag": True, "nothing": None},
            "happened_at": PAYLOAD_DATETIME,
            "calendar_day": date(2026, 9, 19),
            "tags": ["弹幕", "gift", "второй"],
            "nested": {"deep": {"values": [1, 2.5, False]}},
        },
    )


def build_response_error() -> Envelope:
    return Envelope(
        protocol_version="1.0.0",
        request_id=7,
        message_type=MessageType.RESPONSE,
        method="capability.request",
        plugin_id="maibot-live.world-minecraft",
        timestamp_ms=1_726_600_001_000,
        timeout_ms=5_000,
        payload={},
        error={"code": "capability_unavailable", "message": "能力未实现", "details": {"capability": "world.event"}},
    )


def build_broadcast() -> Envelope:
    return Envelope(
        protocol_version="1.0.0",
        request_id=1,
        message_type=MessageType.BROADCAST,
        payload={"event": "plugin.config_updated"},
    )


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    codec = MsgPackCodec()

    samples = {
        "request_envelope": build_request(),
        "response_error": build_response_error(),
        "broadcast_envelope": build_broadcast(),
    }

    meta = {}
    for name, envelope in samples.items():
        encoded = codec.encode_envelope(envelope)
        (OUT_DIR / f"{name}.hex").write_text(encoded.hex(), encoding="ascii")
        meta[name] = {
            "message_type": envelope.message_type.value,
            "method": envelope.method,
            "plugin_id": envelope.plugin_id,
            "request_id": envelope.request_id,
            "protocol_version": envelope.protocol_version,
            "timeout_ms": envelope.timeout_ms,
            "byte_len": len(encoded),
        }

    framed = b"".join(
        len(codec.encode_envelope(envelope)).to_bytes(4, "big") + codec.encode_envelope(envelope)
        for envelope in (build_request(),)
    )
    (OUT_DIR / "framed_request.hex").write_text(framed.hex(), encoding="ascii")

    (OUT_DIR / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已写出 {len(samples) + 1} 个黄金样例到 {OUT_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
