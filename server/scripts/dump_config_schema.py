# -*- coding: utf-8 -*-
"""导出主程序配置的 JSON Schema + 默认值（供 TS 侧做结构化写入的类型强转）。

用法：在仓库根目录执行  python server/scripts/dump_config_schema.py

产物：server/src/config/schema-dump.json
{
  "bot": {"schema": {...JSON Schema...}, "defaults": {节: {键: 默认值}}},
  "model": {"schema": ..., "defaults": ...},
  "sections": {section_name: {"schema": ..., "defaults": ...}}
}

schema 由 pydantic ConfigSchemaGenerator 生成（与 GET /schema/* 端点完全同源），
defaults 从 ConfigBase 的字段默认值提取（供 TS 做新增键的类型推断）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, get_args, get_origin, get_type_hints

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from src.config.config import Config  # noqa: E402
from src.config.official_configs import ConfigBase  # noqa: E402
from src.webui.routers.config import _get_cached_schema  # noqa: E402

SECTION_CLASSES = {
    "bot": "BotConfig",
    "personality": "PersonalityConfig",
    "chat": "ChatConfig",
    "visual": "VisualConfig",
    "message_receive": "MessageReceiveConfig",
    "expression": "ExpressionConfig",
    "jargon": "JargonConfig",
    "keyword_reaction": "KeywordReactionConfig",
    "chinese_typo": "ChineseTypoConfig",
    "response_post_process": "ResponsePostProcessConfig",
    "response_splitter": "ResponseSplitterConfig",
    "telemetry": "TelemetryConfig",
    "log": "LogConfig",
    "maim_message": "MaimMessageConfig",
    "webui": "WebUIConfig",
    "plugin": "PluginConfig",
    "plugin_runtime": "PluginRuntimeConfig",
    "a_memorix": "AMemorixConfig",
    "debug": "DebugConfig",
    "voice": "VoiceConfig",
}

def _import_section_class(class_name: str):
    import src.config.official_configs as oc
    return getattr(oc, class_name, None)

SECTION_MAP = {name: _import_section_class(name) for name in SECTION_CLASSES}


def main() -> int:
    
    dump: Dict[str, Any] = {}

    for name, cls in [("bot", Config),]:
        dump[name] = {
            "schema": _get_cached_schema(name, cls),
            
        }

    sections: Dict[str, Any] = {}
    for section_name, cls in SECTION_MAP.items():
        if cls is None:
            continue
        try:
            sections[section_name] = {
                "schema": _get_cached_schema(f"section:{section_name}", cls, include_nested=False),
            }
        except Exception as exc:
            print(f"跳过 {section_name}: {exc}", file=sys.stderr)

    dump["sections"] = sections

    out_path = Path(__file__).resolve().parent.parent / "src" / "config" / "schema-dump.json"
    out_path.write_text(json.dumps(dump, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"已写出 {out_path}：bot/model + {len(sections)} sections")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
