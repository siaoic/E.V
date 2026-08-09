#!/usr/bin/env python
"""示例 MCP 工具服务器：get_current_time（stdio JSON-RPC）。

启动方式：python tools/example_time.py（stdio 管道由 MCPManager 拉起）。

协议：MCP stdio 传输 = 每行一个 JSON-RPC 2.0 消息。
  - initialize       → 服务端信息
  - notifications/initialized → 无响应（notification）
  - tools/list       → 工具清单（OpenAI function format）
  - tools/call       → 执行工具并返回结果
"""

import json
import sys
from datetime import datetime

SERVER_NAME = "example_time"
SERVER_VERSION = "1.0.0"

TOOL = {
    "name": "get_current_time",
    "description": "获取当前本地时间（示例工具，供 MCP 链路测试）。",
    "inputSchema": {
        "type": "object",
        "properties": {
            "timezone": {
                "type": "string",
                "description": "可选：IANA 时区名（如 Asia/Shanghai），留空用本地时间。",
            }
        },
    },
}


def _handle(msg: dict) -> dict:
    method = msg.get("method")
    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": msg.get("id"),
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        }
    if method == "notifications/initialized":
        return None  # notification：无响应
    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": msg.get("id"),
            "result": {"tools": [TOOL]},
        }
    if method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name")
        args = params.get("arguments") or {}
        if name == "get_current_time":
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            content = (
                f"当前时间（本地）：{now}\n"
                f"timezone 参数（未实际转换，示例工具）：{args.get('timezone') or 'local'}"
            )
            result = {
                "jsonrpc": "2.0",
                "id": msg.get("id"),
                "result": {
                    "content": [{"type": "text", "text": content}],
                    "isError": False,
                },
            }
            return result
        return {
            "jsonrpc": "2.0",
            "id": msg.get("id"),
            "result": {
                "content": [{"type": "text", "text": f"未知工具: {name}"}],
                "isError": True,
            },
        }
    # 未知方法：标准错误响应
    return {
        "jsonrpc": "2.0",
        "id": msg.get("id"),
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        resp = _handle(msg)
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
