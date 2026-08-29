# -*- coding: utf-8 -*-
"""E.V 全链路延迟压测 harness（V3.0，对应 docs/测试任务总览_V3.0.md）。

三种用法：
  1) 用例清单   python scripts/latency_test_harness.py list [--filter C0]
  2) 实时驱动   python scripts/latency_test_harness.py run [--repeat 3] [--filter C0]
                —— spawn `python -u main.py`，逐条注入用例，实时解析 stdout 与字幕 SSE，
                   写 logs/latency_e2e_*.jsonl
  3) 离线解析   python scripts/latency_test_harness.py parse <捕获的控制台日志>
                —— 从日志提取 ⏱ PerfTracker 报告 / 🔧 工具调用行，汇总成 JSONL

解析依据（程序内既有埋点，不侵入主程序）：
  - AI 对话输出被零宽标记 U+2060 包裹（console.CHAT_TAG）→ 首个可见字时刻
  - 「===== 🔧 第 N 轮工具调用 =====」/「AI调用了：xxx 工具 输入参数：…」→ 工具链与时刻
  - PerfTracker 报告：⏱ 本轮对话 / ⏱ LLM，行格式「<label>…<float> ms」→ 内部权威延迟
  - 字幕 SSE(127.0.0.1:8765/events) 的 event: text 在 TTS 首块出声时推送 → first_sound 代理信号
"""

from __future__ import annotations

import argparse
import json
import os
import re
import queue
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CASES_PATH = REPO / "scripts" / "latency_cases.json"
LOGS_DIR = REPO / "logs"

CHAT_TAG = "\u2060"                                   # console.CHAT_TAG
RE_TOOL_CALL = re.compile(r"AI调用了：\s*([^\s：]+?)\s*工具")
RE_TOOL_ROUND = re.compile(r"第\s*(\d+)\s*轮工具调用")
RE_PERF_LINE = re.compile(r"^\s+(.+?)\s+([\d.]+)\s*ms\s*$")
RE_SSE_EVENT = re.compile(r"^event:\s*(\S+)", re.M)

TZ8 = timezone(timedelta(hours=8))


def now_ms() -> float:
    return time.perf_counter() * 1000.0


def iso_now() -> str:
    return datetime.now(TZ8).isoformat(timespec="milliseconds")


# --------------------------------------------------------------------------
# 用例表
# --------------------------------------------------------------------------

def load_cases() -> list[dict]:
    with open(CASES_PATH, encoding="utf-8") as f:
        data = json.load(f)
    return data["cases"]


def filter_cases(cases: list[dict], pattern: str | None) -> list[dict]:
    if not pattern:
        return cases
    pats = [p.strip().lower() for p in pattern.split(",") if p.strip()]
    return [c for c in cases
            if any(p in c["id"].lower() or p in c.get("section", "").lower() for p in pats)]


def cmd_list(args) -> int:
    cases = filter_cases(load_cases(), args.filter)
    print(f"{'ID':<6}{'区':<10}{'预期工具链':<34}红线(first_sound/tool_exec)")
    for c in cases:
        chain = "→".join(c.get("expect_tools", [])) or "（纯文本）"
        gates = c.get("gates", {})
        gate_str = "/".join(f"{k.replace('_ms','')}={v}" for k, v in gates.items() if v)
        print(f"{c['id']:<6}{c.get('section', ''):<10}{chain:<34}{gate_str}")
    print(f"\n共 {len(cases)} 条用例。run 模式示例："
          f"python scripts/latency_test_harness.py run --filter C0 --repeat 3")
    return 0


# --------------------------------------------------------------------------
# 每轮对话的实时采集器（run 模式）
# --------------------------------------------------------------------------

class TurnCapture:
    """监听一行 stdout / 一个 SSE 事件，维护「本轮对话」的延迟采样。"""

    def __init__(self, t0: float, channel: str = "text"):
        self.t0 = t0                  # 注入输入的时刻（perf_counter ms）
        self.channel = channel
        self.first_text_ms: float | None = None
        self.first_sound_ms: float | None = None   # SSE text 事件（TTS 首块出声代理）
        self.saw_clear = False                     # SSE clear 事件（播放完毕=对话结束）
        self.clear_rel_ms: float | None = None
        self.saw_report = False                    # ⏱ 本轮对话 报告已打印
        self.report_rel_ms: float | None = None
        self.last_prompt_rel: float | None = None  # 「你 > 」提示符（wait_input 重入）
        self.tool_rounds: list[int] = []
        self.tool_events: list[dict] = []          # {tool, rel_ms}
        self.perf: dict[str, float] = {}           # ⏱ label → ms（多轮取最后）
        self.raw_tail: list[str] = []              # 控制台转录（诊断用）

    def feed_line(self, line: str) -> None:
        rel = now_ms() - self.t0
        line = re.sub(r"\x1b\[[0-9;]*m", "", line)   # 剥 ANSI 颜色码（否则 ⏱ 解析失败）
        if line.strip():
            self.raw_tail.append(f"[{rel:8.0f}ms] {line.rstrip()}")
            if len(self.raw_tail) > 500:
                self.raw_tail.pop(0)
        if "⏱" in line and "本轮对话" in line:
            self.saw_report = True
            if self.report_rel_ms is None:
                self.report_rel_ms = rel
        if "你 >" in line:
            self.last_prompt_rel = rel
        if self.first_text_ms is None and CHAT_TAG in line:
            # 零宽标记包裹的是 AI 发言；剥掉提示符前缀（你 > / 主动对话：）
            # 后若还有可读内容，才算真正的首个可见字
            stripped = line.replace(CHAT_TAG, "")
            stripped = re.sub(r"你\s*>|主动对话：", "", stripped).strip()
            if stripped:
                self.first_text_ms = rel
        for m in RE_TOOL_CALL.finditer(line):
            self.tool_events.append({"tool": m.group(1), "rel_ms": round(rel, 1)})
        m = RE_TOOL_ROUND.search(line)
        if m:
            self.tool_rounds.append(int(m.group(1)))
        pm = RE_PERF_LINE.match(line)
        if pm:
            try:
                self.perf[pm.group(1)] = float(pm.group(2))
            except ValueError:
                pass


class SseListener(threading.Thread):
    """后台线程：订阅字幕 SSE，把 event:text 时刻回填到当前 TurnCapture。"""

    def __init__(self, port: int):
        super().__init__(daemon=True)
        self.port = port
        self.current: TurnCapture | None = None
        self._stop = threading.Event()

    def run(self) -> None:
        import urllib.request
        url = f"http://127.0.0.1:{self.port}/events"
        while not self._stop.is_set():
            try:
                with urllib.request.urlopen(url, timeout=5) as resp:
                    for raw in resp:
                        if self._stop.is_set():
                            return
                        line = raw.decode("utf-8", errors="replace").strip()
                        if line.startswith("event:"):
                            cap = self.current
                            if "text" in line:
                                if cap is not None and cap.first_sound_ms is None:
                                    cap.first_sound_ms = now_ms() - cap.t0
                            elif "clear" in line and cap is not None:
                                if not cap.saw_clear:
                                    cap.saw_clear = True
                                    cap.clear_rel_ms = now_ms() - cap.t0
            except Exception:
                time.sleep(1.0)   # 字幕服务未起/断线 → 静默重连

    def stop(self) -> None:
        self._stop.set()


def verdict(case: dict, cap: TurnCapture, repeat_note: str = "") -> dict:
    gates = case.get("gates", {}) or {}
    forb = case.get("forbid_tools", []) or []
    fired = [e["tool"] for e in cap.tool_events]

    def _gate(name: str) -> tuple[bool, float | None]:
        limit = gates.get(name)
        actual = getattr(cap, name, None)
        if limit is None or actual is None:
            return True, actual
        return actual <= limit, round(actual, 1)

    checks, slo_pass, unverified = [], True, []
    for g in ("first_text_ms", "first_sound_ms", "tool_exec_ms", "interrupt_stop_ms"):
        ok, actual = _gate(g)
        if gates.get(g) is not None and actual is None:
            unverified.append(g)                 # 定义了红线但本次没测到 → 单独标记
            continue
        if actual is not None:
            checks.append({"gate": g, "limit": gates.get(g), "actual": actual, "pass": ok})
            slo_pass &= ok
    forbidden_fired = sorted(set(fired) & set(forb))
    if forbidden_fired:
        slo_pass = False
    tool_chain = fired
    exp = case.get("expect_tools", []) or []
    chain_match = all(t in tool_chain for t in exp) if exp else True
    # 工具链总耗时：首个工具调用 → 最后一个工具事件（粗粒度，含回喂 LLM 时间）
    if len(cap.tool_events) >= 1:
        first_t = cap.tool_events[0]["rel_ms"]
        last_t = cap.tool_events[-1]["rel_ms"]
        cap.tool_exec_ms = max(0.0, last_t - first_t) if len(cap.tool_events) > 1 else None

    return {
        "timestamp": iso_now(),
        "case_id": case["id"],
        "section": case.get("section", ""),
        "input_channel": cap.channel,
        "input_text": case.get("prompt", case.get("interrupt_prompt", "")),
        "trigger_type": (exp[0] if exp else ("chat" if not forb else "refusal")),
        "tool_chain": tool_chain,
        "tool_chain_expected": exp,
        "tool_chain_match": chain_match,
        "tool_called": tool_chain[-1] if tool_chain else None,
        "tool_events": cap.tool_events,
        "first_text_ms": round(cap.first_text_ms, 1) if cap.first_text_ms is not None else None,
        "first_sound_ms": round(cap.first_sound_ms, 1) if cap.first_sound_ms is not None else None,
        "perf_internal": cap.perf,          # 程序内 PerfTracker 权威值（首字延迟/端到端…）
        "permission_denied": False,
        "forbidden_tool_fired": bool(forbidden_fired),
        "forbidden_detail": forbidden_fired,
        "errors": [],
        "slo_pass": slo_pass,
        "gate_checks": checks,
        "unverified_gates": unverified,
        "verdict": ("FAIL" if (not slo_pass or not chain_match)
                    else "PASS" if not unverified else "UNVERIFIED"),
        "note": repeat_note,
    }


# --------------------------------------------------------------------------
# run 模式
# --------------------------------------------------------------------------

def cmd_run(args) -> int:
    cases = filter_cases(load_cases(), args.filter)
    if not cases:
        print("[harness] 没有匹配的用例")
        return 1
    LOGS_DIR.mkdir(exist_ok=True)
    out_path = LOGS_DIR / f"latency_e2e_{datetime.now(TZ8):%Y%m%d_%H%M%S}.jsonl"

    cmd = args.cmd.split() if args.cmd else [sys.executable, "-u", "main.py"]
    print(f"[harness] 启动主程序：{' '.join(cmd)}")
    proc = subprocess.Popen(cmd, cwd=str(REPO), stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    out_q: "queue.Queue[bytes | None]" = queue.Queue()

    def _reader(pipe, q):            # 后台读线程：空闲检测/退出检测不被 read 阻塞
        for chunk in iter(lambda: pipe.read1(4096) or None, None):
            q.put(chunk)
        q.put(None)

    threading.Thread(target=_reader, args=(proc.stdout, out_q), daemon=True).start()
    sse = SseListener(args.sse_port)
    if not args.no_sse:
        sse.start()

    def _write_stdin(text: str) -> None:
        # stdin 编码默认 cp936：主程序 input() 按 locale(gbk)+surrogateescape 解码
        # （控制中心按 utf-8 写入存在乱码 bug，见测试报告），harness 与子进程
        # 解码端对齐以保证注入文本语义无损
        try:
            proc.stdin.write((text + "\n").encode(args.stdin_encoding))
            proc.stdin.flush()
        except Exception as e:
            print(f"[harness] stdin 写入失败（主程序可能已退出）：{e}")

    results: list[dict] = []
    interrupter: threading.Timer | None = None
    try:
        # 就绪探测：工具与 TTS 都就绪才开始（warmup 为上限）。
        # 只看「工具已就绪」会在 TTS 后台加载完成前开跑 → 首轮被模型加载污染
        tools_ready = tts_ready = False
        buf = b""
        deadline = time.time() + args.warmup
        ready = False
        while time.time() < deadline:
            try:
                chunk = out_q.get(timeout=1.0)
            except queue.Empty:
                continue
            if chunk is None:
                break
            buf += chunk
            text_tail = buf.decode("utf-8", errors="replace")[-6000:]
            if "工具已就绪" in text_tail:
                tools_ready = True
            if "TTS 预热完成" in text_tail or "TTS 模型加载完成" in text_tail:
                tts_ready = True
            if tools_ready and tts_ready:
                ready = True
                break
        print(f"[harness] 主程序{'就绪' if ready else '未在时限内就绪（继续）'}"
              f"（工具={tools_ready} TTS={tts_ready}），开始注入用例")

        warm = {"id": "WARM", "section": "warmup", "prompt": "你好呀，随便聊两句",
                "expect_tools": [], "forbid_tools": [], "gates": {}}
        for rep in range(1, args.repeat + 1):
            run_cases = ([warm] + cases) if rep == 1 else cases
            for case in run_cases:
                if proc.poll() is not None:
                    print("[harness] 主程序已退出，提前结束")
                    break
                # 用例间排水：丢弃上一轮未流尽的 stdout 尾巴，防止串进本轮采集
                drain_until = time.time() + args.gap
                while time.time() < drain_until:
                    try:
                        if out_q.get(timeout=0.4) is None:
                            break
                    except queue.Empty:
                        continue
                cap = TurnCapture(t0=now_ms())
                sse.current = cap
                tag = "★预热" if case["id"] == "WARM" else f"第{rep}轮"
                print(f"\n[harness] ▶ {case['id']}（{tag}）: {case['prompt']}")
                _write_stdin(case["prompt"])

                if case.get("interrupt_after_s") is not None:
                    ip = case.get("interrupt_prompt")
                    def _fire_interrupt():
                        print(f"[harness] ⚡ 打断注入: {ip}")
                        _write_stdin(ip)
                    interrupter = threading.Timer(case["interrupt_after_s"], _fire_interrupt)
                    interrupter.start()

                buf = b""
                last_activity = time.time()
                dead = False
                self_t0 = time.time()
                while True:
                    try:
                        chunk = out_q.get(timeout=args.idle)
                    except queue.Empty:
                        end_reason = "idle_timeout"
                        break                       # 空闲超时 → 本轮结束
                    if chunk is None:
                        dead = True                 # 主程序 stdout 关闭
                        end_reason = "proc_exit"
                        break
                    buf += chunk
                    last_activity = time.time()
                    *lines, buf = buf.replace(b"\r\n", b"\n").replace(b"\r", b"\n").partition(b"\n")
                    for raw in lines:
                        cap.feed_line(raw.decode("utf-8", errors="replace"))
                    # 打字机 delta 无换行：部分缓冲区一出现首字标记立即喂入，
                    # 否则 first_text 会被推迟到整句结束
                    if cap.first_text_ms is None and CHAT_TAG.encode("utf-8") in buf:
                        cap.feed_line(buf.decode("utf-8", errors="replace"))
                    now = time.time()
                    # 轮次边界（按可信度排序）：
                    # ① 完成信号（⏱ 报告 或 SSE clear）之后，主循环重新打印
                    #    「你 > 」提示符 = 已回到 wait_input → 本轮彻底结束
                    signal_rel = next((v for v in (cap.report_rel_ms, cap.clear_rel_ms)
                                       if v is not None), None)
                    if (signal_rel is not None and cap.last_prompt_rel is not None
                            and cap.last_prompt_rel >= signal_rel + 1000):
                        end_reason = "prompt_ready"
                        break
                    # ② 空闲兜底 / ③ 硬上限（防内联长工具把窗口拖死）
                    if now - last_activity > args.idle:
                        end_reason = "idle_timeout"
                        break
                    if now - self_t0 > args.hard_cap:
                        end_reason = "hard_cap"
                        break
                cap.feed_line(buf.decode("utf-8", errors="replace"))
                rec = verdict(case, cap, repeat_note=f"rep{rep}")
                rec["turn_end_ms"] = round(cap.clear_rel_ms, 1) if cap.clear_rel_ms else None
                rec["end_reason"] = end_reason
                rec["transcript"] = cap.raw_tail
                results.append(rec)
                print(f"[harness] ■ {case['id']} → {rec['verdict']}  "
                      f"首字={rec['first_text_ms']}ms 出声={rec['first_sound_ms']}ms "
                      f"工具={rec['tool_chain'] or '无'}")
                with open(out_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                if dead:
                    print("[harness] 主程序已退出，提前结束")
                    break
    except KeyboardInterrupt:
        print("\n[harness] 手动中断，收尾写出已有结果")
    finally:
        if interrupter:
            interrupter.cancel()
        sse.stop()
        try:
            proc.terminate()
        except Exception:
            pass
    _summary(results, out_path)
    return 0


# --------------------------------------------------------------------------
# parse 模式（离线）
# --------------------------------------------------------------------------

def cmd_parse(args) -> int:
    """离线解析：以「⏱ 本轮对话」块为轮次边界（它在每轮结束时打印），
    该轮内所有 ⏱ 块（本轮对话/LLM/TTS…）指标合并、工具调用行归属当轮。"""
    text = Path(args.logfile).read_text(encoding="utf-8", errors="replace")
    LOGS_DIR.mkdir(exist_ok=True)
    out_path = LOGS_DIR / f"latency_parsed_{datetime.now(TZ8):%Y%m%d_%H%M%S}.jsonl"

    def _flush(turn: dict, out: list) -> None:
        if turn.get("perf_internal") or turn.get("tool_chain"):
            out.append({"timestamp": iso_now(), **turn})

    results: list[dict] = []
    turn: dict = {"turn": 1, "tool_chain": [], "perf_internal": {}}
    in_perf = False
    pending_flush = False          # 遇到「⏱ 本轮对话」=当轮收尾；其指标行仍属当轮，
    for ln in text.splitlines():   # flush 延迟到下一个非指标行（下一轮内容开始时）
        if "⏱" in ln:
            if "本轮对话" in ln:
                pending_flush = True
            in_perf = True
            continue
        if in_perf:
            pm = RE_PERF_LINE.match(ln)
            if pm:
                label, val = pm.group(1).strip(), float(pm.group(2))
                if label in turn["perf_internal"]:    # 跨块同名 → 加后缀防覆盖
                    label = f"{label}(2)"
                turn["perf_internal"][label] = val
                continue
            if ln.strip():
                in_perf = False
            else:
                continue
        if pending_flush:
            _flush(turn, results)
            turn = {"turn": len(results) + 1, "tool_chain": [], "perf_internal": {}}
            pending_flush = False
        for m in RE_TOOL_CALL.finditer(ln):
            turn["tool_chain"].append(m.group(1))
    if pending_flush:
        _flush(turn, results)

    with open(out_path, "w", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    _summary(results, out_path)
    return 0


# --------------------------------------------------------------------------
# 汇总
# --------------------------------------------------------------------------

def _summary(results: list, out_path: Path) -> None:
    if not results:
        print("[harness] 无采样结果")
        return
    print(f"\n===== 延迟汇总（{len(results)} 轮，明细: {out_path}）=====")
    print(f"{'轮':<8}{'首字ms':>9}{'出声ms':>9}{'端到端ms':>10}  工具链 / 判定")
    for r in results:
        perf = r.get("perf_internal", {})
        ft = r.get("first_text_ms")
        fs = r.get("first_sound_ms")
        e2e = perf.get("端到端") or "--"
        chain = "→".join(r.get("tool_chain", []) or []) or "（纯文本）"
        v = r.get("verdict", "-")
        print(f"{str(r.get('case_id') or ('#' + str(r.get('turn', '?')))):<8}"
              f"{ft if ft is not None else '--':>9}"
              f"{fs if fs is not None else '--':>9}"
              f"{e2e if not isinstance(e2e, str) else '--':>10}  {chain[:52]} / {v}")
    # 分指标 p50（严格同名来源，避免内部/外部口径混算）
    for label, src in (("first_text_ms(外)", "first_text_ms"),
                       ("first_sound_ms(外)", "first_sound_ms"),
                       ("首字延迟(内)", "首字延迟"),
                       ("端到端(内)", "端到端")):
        vals = sorted(x for r in results
                      for x in [r.get(src) or r.get("perf_internal", {}).get(src)]
                      if x is not None)
        if len(vals) >= 2:
            print(f"全体 {label}: p50={vals[len(vals) // 2]:.0f}ms  "
                  f"min={vals[0]:.0f}  max={vals[-1]:.0f}  n={len(vals)}")


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="E.V 全链路延迟压测 harness")
    sub = ap.add_subparsers(dest="mode", required=True)

    p = sub.add_parser("list", help="列出用例")
    p.add_argument("--filter", help="按 id/区名过滤，逗号分隔")
    p.set_defaults(fn=cmd_list)

    p = sub.add_parser("run", help="启动主程序并自动驱动用例")
    p.add_argument("--filter", help="按 id/区名过滤，如 C0、T1")
    p.add_argument("--repeat", type=int, default=1, help="每条用例重复轮数（取 p50 用）")
    p.add_argument("--cmd", default="", help="自定义启动命令，默认 python -u main.py")
    p.add_argument("--idle", type=float, default=90.0, help="输出空闲多少秒判定本轮结束（兜底，需容纳内联 OMR）")
    p.add_argument("--hard-cap", type=float, default=240.0, help="单用例硬上限秒数")
    p.add_argument("--gap", type=float, default=2.0, help="用例之间的排水间隔秒数")
    p.add_argument("--stdin-encoding", default="cp936",
                   help="写子进程 stdin 的编码（默认 cp936，与主程序 input() 的 locale 解码对齐）")
    p.add_argument("--warmup", type=float, default=45.0, help="启动后等待就绪秒数")
    p.add_argument("--sse-port", type=int, default=8765, help="字幕 SSE 端口（出声信号）")
    p.add_argument("--no-sse", action="store_true", help="禁用 SSE 监听")
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("parse", help="离线解析捕获的控制台日志")
    p.add_argument("logfile")
    p.set_defaults(fn=cmd_parse)

    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
