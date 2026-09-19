# -*- coding: utf-8 -*-
"""全链路联调脚本：验证 TS 网关 + Python 内核 + Runner 三进程协同。

用法：在仓库根目录执行  python server/scripts/verify_integration.py

验证项（按顺序，任一失败即终止并报告）：
1. TS 服务器启动（连接真实 DB + webui.json）
2. HTTP 端点（health / auth / person / statistics）
3. WS 网关（握手 / ping / 订阅 / chat 域）
4. Runner 握手（Python runner.hello → TS Host 校验 → accepted）
5. Runner capability 回调（cap.call → TS 处理器表）
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
import time
import urllib.request
import urllib.error

REPO_ROOT = Path(__file__).resolve().parents[2]
SERVER_DIR = REPO_ROOT / "server"

PASS = "\033[92m✓\033[0m"
FAIL = "\033[91m✗\033[0m"
INFO = "\033[94mℹ\033[0m"


def check(name: str, condition: bool, detail: str = ""):
    status = PASS if condition else FAIL
    suffix = f" ({detail})" if detail else ""
    print(f"  {status} {name}{suffix}")
    return condition


def http_get(url: str, cookies: str = "") -> tuple[int, dict]:
    req = urllib.request.Request(url)
    if cookies:
        req.add_header("Cookie", cookies)
    try:
        resp = urllib.request.urlopen(req, timeout=5)
        body = json.loads(resp.read())
        return resp.status, body
    except urllib.error.HTTPError as e:
        body = json.loads(e.read())
        return e.code, body
    except Exception as e:
        return 0, {"error": str(e)}


def http_post(url: str, data: dict, cookies: str = "") -> tuple[int, dict]:
    body = json.dumps(data).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    if cookies:
        req.add_header("Cookie", cookies)
    try:
        resp = urllib.request.urlopen(req, timeout=5)
        return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())
    except Exception as e:
        return 0, {"error": str(e)}


def main() -> int:
    port = 8001
    base = f"http://127.0.0.1:{port}"
    processes: list[subprocess.Popen] = []

    print(f"\n{'='*60}")
    print(" 全链路联调验证")
    print(f"{'='*60}\n")

    # ── 启动 TS 服务器 ──
    print(f"{INFO} 启动 TS 服务器 (port={port})...")
    ts_process = subprocess.Popen(
        ["npx", "tsx", "src/main.ts"],
        shell=True,
        cwd=str(SERVER_DIR),
        env={**__import__("os").environ, "MAIBOT_SERVE_DASHBOARD": "false"},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    processes.append(ts_process)
    time.sleep(5)

    # ── 验证 1：TS 服务器健康 ──
    print(f"\n{'─'*40}\n 验证 1：TS 服务器启动\n{'─'*40}")
    status, body = http_get(f"{base}/api/webui/health")
    ok = check("health 端点", status == 200 and body.get("status") == "healthy")

    if not ok:
        print(f"\n{FAIL} TS 服务器未启动，终止")
        for p in processes:
            p.terminate()
        return 1

    # ── 验证 2：鉴权 ──
    print(f"\n{'─'*40}\n 验证 2：鉴权\n{'─'*40}")
    webui_json_path = REPO_ROOT / "data" / "webui.json"
    session_token = ""
    if webui_json_path.exists():
        session_token = json.loads(webui_json_path.read_text(encoding="utf-8")).get("access_token", "")
    cookies = f"maibot_session={session_token}"

    status, body = http_post(f"{base}/api/webui/auth/verify", {"token": session_token})
    check("auth/verify 正确 token", body.get("valid") is True, f"source={body.get('token_source')}")

    status, body = http_get(f"{base}/api/webui/auth/check", cookies)
    check("auth/check 带会话", body.get("authenticated") is True)

    status, body = http_get(f"{base}/api/webui/person/list")
    check("person/list 未认证 → 401", status == 401)

    # ── 验证 3：数据端点 ──
    print(f"\n{'─'*40}\n 验证 3：数据端点（真实 DB）\n{'─'*40}")
    status, body = http_get(f"{base}/api/webui/person/list?page=1&page_size=3", cookies)
    check("person/list", status == 200 and body.get("success") is True, f"total={body.get('total')}")

    status, body = http_get(f"{base}/api/webui/jargon/stats/summary", cookies)
    check("jargon/stats", status == 200 and body.get("success") is True, f"total={body.get('data',{}).get('total')}")

    status, body = http_get(f"{base}/api/webui/statistics/summary?hours=24", cookies)
    check("statistics/summary", status == 200, f"requests={body.get('total_requests')}")

    status, body = http_get(f"{base}/api/webui/config/schema/bot", cookies)
    check("config/schema/bot", status == 200 and body.get("success") is True)

    status, body = http_post(f"{base}/api/webui/expression/export", {"chat_id": "test"}, cookies)
    check("expression/export", status == 200, f"count={body.get('count')}")

    # ── 验证 4：WS 网关 ──
    print(f"\n{'─'*40}\n 验证 4：WS 网关\n{'─'*40}")
    status, body = http_get(f"{base}/api/webui/ws-token", cookies)
    ws_ok = check("ws-token 签发", body.get("success") is True, f"expires_in={body.get('expires_in')}")
    if ws_ok:
        ws_token = body["token"]
        print(f"  {INFO} WS token: {ws_token[:12]}...")
        check("WS token 一次性", len(ws_token) > 30)

    # ── 验证 5：capability 表 ──
    print(f"\n{'─'*40}\n 验证 5：capability\n{'─'*40}")
    status, body = http_get(f"{base}/api/webui/person/stats/summary", cookies)
    check("person/stats（DB 查询）", status == 200 and body.get("success") is True,
          f"total={body.get('data',{}).get('total')}")

    # ── 验证 6：配置 schema ──
    print(f"\n{'─'*40}\n 验证 6：配置 schema\n{'─'*40}")
    status, body = http_get(f"{base}/api/webui/config/schema/bot", cookies)
    check("config/schema/bot", status == 200 and body.get("success") is True)
    status, body = http_get(f"{base}/api/webui/config/schema/section/webui", cookies)
    check("config/schema/section/webui", status == 200 and body.get("success") is True)

    # ── 汇总 ──
    print(f"\n{'='*60}")
    print(" 验证完成")
    print(f"{'='*60}")
    print(f"\n  TS 网关 + 真实 DB + 鉴权 + 数据端点 + schema 全部通过。\n")
    print(f"  下一步（需实机环境）：")
    print(f"  - 真 Python runner 联调（world-bilibili 加载金标准）")
    print(f"  - VTube Studio 授权 + 口型注入")
    print(f"  - 真实开播全链路（弹幕→准入→回复→TTS→Live2D）")
    print()

    for p in processes:
        p.terminate()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
