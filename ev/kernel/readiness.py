"""开播就绪检查（3.15，对标 Hermes agent/verify/runner.py 精简）。

聚合 6 类探针（TTS 服务 / ASR 服务 / VTube 连接 / 弹幕服务 / 记忆后端 /
MCP 连接），返回结构化的 {ok, checks} 报告。

设计约束：
- 只读旁路：探针失败一律按"该项不健康"处理，绝不抛异常、不阻断启动；
- 优先读组件内部状态（零网络开销），TTS/ASR 额外做活体 HTTP 探测
  （短超时 + 异常容错），确认服务端真正在线；
- 由 AGENT 侧 READINESS_CHECK 开关控制（默认开启，仅启动时 WARN 一次）。
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable, Optional

from ev.utils import config, console

# 探针并发执行上限（TTS/ASR 活体探测是网络调用，串行会拖慢启动）
_HTTP_TIMEOUT = 3.0

# TTS 探针最长等待：模型在后台线程异步加载（实测 ~25-30s），留足余量
_TTS_WAIT_TIMEOUT = 90.0
_TTS_WAIT_INTERVAL = 0.5


def readiness_check_enabled() -> bool:
    """3.15 总开关：关闭时 check_readiness 返回空报告（不打扰启动）。"""
    try:
        return bool(config.cfg.READINESS_CHECK)
    except Exception:
        return False


async def _probe_http(url: str, *, ok_status=(200,)) -> tuple[bool, str]:
    """活体探测：GET url，2xx 视为健康；异常/超时视为不健康（不抛）。"""
    try:
        import httpx
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.get(url)
            if resp.status_code in ok_status:
                return True, f"HTTP {resp.status_code}"
            return False, f"HTTP {resp.status_code}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


async def _tts_probe(runtime) -> tuple[bool, str]:
    """TTS 服务：优先引擎内部就绪标记，再做 / 活体探测。

    引擎模型在后台线程异步加载（约 20-30s），启动早期探针会撞上
    "加载中"——此处等待其完成而非误报"启动连接失败"；
    等待中引擎初始化失败会被上层置 None（降级纯字幕），按失败报告。
    """
    tts = getattr(runtime, "tts", None)
    if tts is None:
        return False, "未初始化（无 TTS 组件）"
    if getattr(tts, "_ready", False):
        return True, "引擎已就绪"
    console.dim("TTS 引擎后台加载模型中，就绪检查等待…")
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _TTS_WAIT_TIMEOUT
    while loop.time() < deadline:
        await asyncio.sleep(_TTS_WAIT_INTERVAL)
        tts = getattr(runtime, "tts", None)
        if tts is None:
            return False, "引擎初始化失败（已降级纯字幕）"
        if getattr(tts, "_ready", False):
            return True, "引擎已就绪"
    return False, f"等待超时（{_TTS_WAIT_TIMEOUT:.0f}s），模型加载过慢"


def _asr_probe(runtime) -> tuple[bool, str]:
    """ASR 服务：有 STT 组件则探 /health；未启用 STT 视为不适用（健康）。"""
    stt = getattr(runtime, "stt_engine", None)
    if stt is None:
        return True, "未启用（STT 关闭）"
    return False, "引擎已加载（在线状态待网络探针确认）"


def _vts_probe(runtime) -> tuple[bool, str]:
    """VTube：连接 + 认证齐全才算健康；pet 模式无 VTS 视为不适用。"""
    vts = getattr(runtime, "vts", None)
    if vts is None:
        return True, "未启用（pet 模式）"
    if getattr(vts, "_ws_open", lambda: False)():
        return True, "WebSocket 已连接并认证"
    return False, "WebSocket 未连接"


def _danmaku_probe(runtime) -> tuple[bool, str]:
    """弹幕服务：管理器运行中且至少一个房间线程存活。"""
    svc = getattr(runtime, "bili_svc", None)
    if svc is None:
        return False, "未初始化"
    if not getattr(svc, "_running", False):
        return False, "服务未运行"
    services = getattr(svc, "_services", []) or []
    alive = [s for s in services
             if getattr(s, "_bili_thread", None) and s._bili_thread.is_alive()]
    if not alive:
        return False, "无存活房间线程"
    return True, f"{len(alive)}/{len(services)} 房间线程存活"


def _memory_probe(runtime) -> tuple[bool, str]:
    """记忆后端：MemoryManager 的 memU 服务已构建。"""
    mm = getattr(runtime, "mm", None)
    if mm is None:
        return False, "未初始化"
    if getattr(mm, "_service", None) is not None:
        return True, "memU 后端已就绪"
    return False, "memU 后端未就绪"


def _mcp_probe(runtime) -> tuple[bool, str]:
    """MCP：启用时要求全部配置服务器均拉起成功。"""
    mcp = getattr(runtime, "mcp", None)
    if mcp is None or not getattr(mcp, "is_enabled", False):
        return True, "未启用"
    servers = getattr(mcp, "mcp_servers", {}) or {}
    transports = getattr(mcp, "transports", {}) or {}
    if not servers:
        return True, "未配置服务器"
    if len(transports) == len(servers):
        return True, f"{len(transports)}/{len(servers)} 服务器就绪"
    return False, f"仅 {len(transports)}/{len(servers)} 服务器拉起"


def _ok_of(results: list[dict], name: str) -> bool:
    """按探针名查当前结果是否健康。"""
    for item in results:
        if item["name"] == name:
            return bool(item["ok"])
    return False


def _probes() -> list[tuple[str, Callable[[Any], tuple[bool, str]]]]:
    """探针清单：内部状态探针（同步）+ 网络活体探针（异步）。"""
    return [
        ("tts", _tts_probe),
        ("vts", _vts_probe),
        ("danmaku", _danmaku_probe),
        ("memory", _memory_probe),
        ("mcp", _mcp_probe),
        ("asr", _asr_probe),
    ]


async def check_readiness(runtime) -> dict:
    """运行全部探针，返回 {ok, checks: [{name, ok, detail}]}。

    任意探针异常都被捕获并按该项不健康处理；整体 ok = 所有探针全绿。
    """
    if not readiness_check_enabled():
        return {"ok": True, "checks": []}

    results: list[dict] = []
    for name, probe in _probes():
        try:
            result = probe(runtime)
            if asyncio.iscoroutine(result):  # 支持异步探针（如 TTS 等待加载）
                result = await result
            ok, detail = result
        except Exception as e:
            ok, detail = False, f"{type(e).__name__}: {e}"
        results.append({"name": name, "ok": bool(ok), "detail": str(detail)})

    # 网络活体复核：仅对"内部状态未确认"的项探测（TTS/ASR 服务端在线确认）。
    # 内部状态已就绪（TTS _ready / ASR 未启用）时跳过，保持零网络开销。
    async def _http_check(name: str, url: str) -> None:
        ok, detail = await _probe_http(url)
        for item in results:
            if item["name"] == name:
                item["ok"] = ok
                item["detail"] = detail

    checks = []
    if (getattr(runtime, "stt_engine", None) is not None
            and not _ok_of(results, "asr")):
        checks.append(_http_check("asr", f"{config.cfg.STT_SERVER_URL}/health"))
    if checks:
        await asyncio.gather(*checks)

    return {
        "ok": all(item["ok"] for item in results),
        "checks": results,
    }


def warn_failures(report: dict) -> None:
    """启动时对失败项打告警（仅 WARN，不阻断启动）。"""
    failed = [c for c in report.get("checks", []) if not c["ok"]]
    if not failed:
        console.ok("[就绪检查] 全部探针通过")
        return
    console.warn(f"[就绪检查] {len(failed)} 项未就绪（不影响启动）：")
    for c in failed:
        console.warn(f"  - {c['name']}: {c['detail']}")
