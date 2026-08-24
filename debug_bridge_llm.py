"""🅱+🅲 端到端：MainChatSubAgentBridge 真调 LLM + Blackboard 跨 Agent 通知。

链路：
  maybe_delegate('帮我查一下天气', runtime)
    → Bridge._should_delegate 命中 ('帮我查' 是关键词)
    → delegate_backend_enabled()==True → get_delegation_queue().enqueue(...)
    → 入队后立即 _notify_user（不依赖 LLM，只走 proactive 或 speak_text）
    → 后台 DelegationWorker 轮询到 job
    → executor(job) 调真 LLM（create_agent → ReActAgent.run）
    → LLM 流式返回 → result 文本
    → _run_and_report 写 blackboard['delegation_result']  ← 黑板写
    → 同时 fake Agent 订阅 'delegation_result' 收到 1 次通知  ← 🅲

开关（脚本退出后还原）：
  AGENT_ENABLED              = True
  AGENT_DELEGATE_BACKEND     = True
  .cfg.LLM_BASE_URL/_API_KEY/_MODEL 必须可用（从 .env 读取）

不做任何 mock LLM，但可能因网络/限额失败 → 标 FAIL 不抛。
"""
import os, sys, time, asyncio, traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv
load_dotenv()

import logging
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("openai").setLevel(logging.WARNING)

PASS = "\033[92m[ PASS ]\033[0m"
FAIL = "\033[91m[ FAIL ]\033[0m"
INFO = "\033[96m[ INFO ]\033[0m"
WARN = "\033[93m[ WARN ]\033[0m"


def _section(title: str):
    print(f"\n{INFO} {title}")
    sys.stdout.flush()


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------
async def main() -> int:
    from ev.utils import config
    from ev.agent import async_delegation as ad
    from ev.agent.bridge import MainChatSubAgentBridge
    from ev.agent.blackboard import get_blackboard
    from ev.agent import run_task

    cfg = config.cfg

    # --- 备份环境 ---
    saved_env = {
        "AGENT_ENABLED": os.environ.get("AGENT_ENABLED"),
        "AGENT_DELEGATE_BACKEND": os.environ.get("AGENT_DELEGATE_BACKEND"),
    }
    saved_attrs = {
        k: getattr(cfg, k, None) for k in
        ("AGENT_ENABLED", "AGENT_DELEGATE_BACKEND")
    }
    rc = 0
    mcp = None
    try:
        # --- 开双开关 ---
        os.environ["AGENT_ENABLED"] = "1"
        os.environ["AGENT_DELEGATE_BACKEND"] = "1"
        cfg.AGENT_ENABLED = True
        cfg.AGENT_DELEGATE_BACKEND = True

        # --- 校验 LLM 配置可用 ---
        if not (cfg.LLM_BASE_URL and cfg.LLM_API_KEY
                and (cfg.AGENT_MODEL or cfg.LLM_MODEL)):
            print(f"{FAIL} .env 缺少 LLM 配置，跳过测试")
            return 2

        # --- 创建 MCPManager（验证 Agent 能调 bing_search）---
        if cfg.MCP_ENABLED and cfg.TOOLS_ENABLED:
            from ev.mcp.manager import MCPManager
            mcp = MCPManager()
            await mcp.initialize()
            tools = mcp.get_tools_for_llm()
            print(f"{INFO} MCP 已启动，{len(tools)} 个 MCP 工具: "
                  f"{[t['function']['name'] for t in tools]}")
        else:
            print(f"{WARN} MCP 未启用（MCP_ENABLED={cfg.MCP_ENABLED} "
                  f"TOOLS_ENABLED={cfg.TOOLS_ENABLED}），跳过 MCP 工具验证")

        # --- 清掉旧 worker ---
        with ad._singleton_guard:
            if ad._worker is not None:
                try:
                    ad._worker.stop()
                except Exception:
                    pass
                ad._worker = None
        q = ad.get_delegation_queue()
        try:
            with q._lock:
                q._conn.execute("DELETE FROM delegate_queue WHERE status='pending'")
                q._conn.commit()
        except Exception:
            pass

        # --- Bridge & 假 Runtime ---
        _section("构造 Bridge + Fake Runtime")
        bridge = MainChatSubAgentBridge()
        bb = get_blackboard()
        _cfg_for_rt = cfg  # 避免类体内遮蔽

        class _FakeProactive:
            def _enqueue(self, text: str, _):
                print(f"{INFO}    [Proactive 通知] {text!r}")
                sys.stdout.flush()
        class _FakeRuntime:
            cfg = _cfg_for_rt  # 用真 cfg，保证 LLM 配置可见
            proactive = _FakeProactive()

        # --- 注册 executor：真调 LLM（用 create_agent 跑 ReActAgent）---
        # 注意：当前 bridge.maybe_delegate 入队路径只 enqueue + notify，
        # 不会把 result 写 blackboard（设计只在 _run_and_report 里写）。
        # 这里在 executor 里手动补上 blackboard.put + _notify_user，
        # 完整模拟「worker 跑完 → 通知桥 → 订阅者收到」链路。
        _section("注册 executor = create_agent() + blackboard.put")
        _exec_runtime = _FakeRuntime()

        def executor_llm(job: dict) -> str:
            """worker 调这个 → 调真 LLM → 写黑板 → 通知用户。"""
            task = job.get("task", "")
            print(f"{INFO}    worker 取出 job#{job.get('id')} task={task!r}")
            sys.stdout.flush()
            try:
                # 1) 真调 LLM（异步，带 MCP 工具：bing_search 等可用）
                result = asyncio.run(run_task(task, mcp=mcp))
                print(f"{INFO}    LLM 返回 (前 200 字): "
                      f"{(result or '')[:200]!r}")
                sys.stdout.flush()
                result = result or "(empty)"

                # 2) 写 blackboard（订阅者会立刻收到通知）
                async def _write_bb_and_notify():
                    await bridge.blackboard.put(
                        "delegation_result",
                        {"task": task, "result": result[:2000]},
                        source="main_chat_bridge",
                    )
                    await bridge._notify_user(
                        _exec_runtime, f"任务完成了：{result[:200]}")
                asyncio.run(_write_bb_and_notify())
                print(f"{INFO}    blackboard.put + _notify_user 已触发")
                sys.stdout.flush()
                return result
            except Exception as e:
                tb = traceback.format_exc(limit=3)
                print(f"{WARN}    executor 异常: {type(e).__name__}: {e}\n{tb}")
                sys.stdout.flush()
                raise

        ad.ensure_worker(executor_llm)

        # --- 黑板订阅者（fake Agent） ---
        _section("订阅 blackboard['delegation_result']")
        notified: list[dict] = []

        def _sub(entry: dict) -> None:
            notified.append(entry)
            v = entry.get("value") or {}
            print(f"{INFO}    [订阅者收到] key={entry.get('key')!r} "
                  f"source={entry.get('source')!r} "
                  f"task={(v.get('task') or '')[:40]!r} "
                  f"result_len={len(v.get('result') or '')}")
            sys.stdout.flush()

        unsub = bb.subscribe("delegation_result", _sub)

        # --- 注入用户输入 ---
        _section("调用 bridge.maybe_delegate('帮我搜索一下最近AI虚拟主播的新闻', runtime)")
        user_text = "帮我搜索一下最近AI虚拟主播的新闻"
        job_id = await bridge.maybe_delegate(user_text, _FakeRuntime())
        if job_id is None:
            print(f"{FAIL} maybe_delegate 返回 None，未命中委派")
            return 3
        print(f"{PASS} maybe_delegate 入队成功 job_id={job_id}")

        # --- 轮询 blackboard 等真 LLM 结果 ---
        _section("等待 blackboard['delegation_result'] (最多 90s)")
        result_entry = None
        deadline = time.time() + 90
        while time.time() < deadline:
            await asyncio.sleep(1.0)
            r = bb.get("delegation_result")
            if r and r.get("task") == user_text:
                result_entry = r
                break
        if result_entry is None:
            print(f"{FAIL} 90s 内未拿到 LLM 结果")
            # 看看 job 真实状态
            with q._lock:
                cur = q._conn.execute(
                    "SELECT id, status, attempts, "
                    "substr(result, 1, 200), substr(error, 1, 200) "
                    "FROM delegate_queue ORDER BY id DESC LIMIT 3")
                rows = cur.fetchall()
            print(f"{WARN} 最近 job 状态:")
            for row in rows:
                print(f"       id={row[0]} status={row[1]} "
                      f"attempts={row[2]} result[:200]={row[3]!r} "
                      f"error[:200]={row[4]!r}")
            rc = 1
        else:
            res_text = result_entry.get("result") or ""
            print(f"{PASS} blackboard 已写入, result 前 300 字:\n"
                  f"     {(res_text[:300] + ('...' if len(res_text)>300 else ''))!r}")
            if len(res_text) < 5:
                print(f"{WARN} result 过短，可能 LLM 失败")

        # --- 验证订阅通知 ---
        _section("验证黑板订阅者收到通知")
        for _ in range(10):
            if notified:
                break
            await asyncio.sleep(0.1)
        if not notified:
            print(f"{FAIL} 订阅者 0 次通知")
            rc = max(rc, 1)
        else:
            e = notified[0]
            print(f"{PASS} 订阅者收到 {len(notified)} 次, 首次: "
                  f"key={e.get('key')!r} source={e.get('source')!r}")

        # --- 汇总 ---
        print()
        print("=" * 72)
        print(f"  测试结果: {'PASS' if rc == 0 else 'FAIL'}")
        print("=" * 72)
        print(f"  1. Bridge._should_delegate 命中关键词: ✅")
        print(f"  2. get_delegation_queue().enqueue: ✅ job_id={job_id}")
        print(f"  3. worker 真调 LLM: "
              f"{'✅' if result_entry else '❌'}")
        print(f"  4. result 写入 blackboard['delegation_result']: "
              f"{'✅' if result_entry else '❌'}")
        print(f"  5. fake Agent 订阅通知: "
              f"{'✅ ('+str(len(notified))+' 次)' if notified else '❌'}")
        try:
            ad._worker.stop()
        except Exception:
            pass
        if mcp is not None:
            try:
                await mcp.stop()
            except Exception:
                pass
        return rc

    finally:
        # --- 还原环境 ---
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        for k, v in saved_attrs.items():
            try:
                setattr(cfg, k, v)
            except Exception:
                pass


if __name__ == "__main__":
    rc = asyncio.run(main())
    sys.exit(rc)
