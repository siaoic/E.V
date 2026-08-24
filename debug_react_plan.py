"""定位 ReActAgent 中 DeepSeek 思考模式 400 的具体轮次与消息结构。

monkey-patch ReActAgent._build_messages，打印每轮重建的 messages；
再用真实 LLM 跑一轮会触发工具调用的任务，观察第几轮 400。
"""
import os, sys, asyncio, json, traceback
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


def _brief(messages):
    """压缩打印 messages 结构（每条的 role/有无 tool_calls/有无 reasoning_content）。"""
    out = []
    for m in messages:
        role = m.get("role")
        tcs = len(m.get("tool_calls") or [])
        rc = "reasoning_content" in m and (m.get("reasoning_content") or "")
        rclen = len(rc) if rc else 0
        content = (m.get("content") or "")
        out.append(
            f"{role}" + (f"[tcs={tcs}]" if tcs else "")
            + (f"[rc={rclen}]" if rclen else "")
            + f" content={content[:40]!r}")
    return "\n    ".join(out)


async def main() -> int:
    from ev.agent import create_agent, ReActAgent

    # --- monkey-patch _build_messages 打日志 ---
    orig_build = ReActAgent._build_messages
    def patched(self):
        msgs = orig_build(self)
        print(f"{INFO}  _build_messages ->\n    {_brief(msgs)}")
        sys.stdout.flush()
        return msgs
    ReActAgent._build_messages = patched

    agent = create_agent()
    print(f"{INFO} Agent 工具: {[s['name'] for s in agent._executor.schemas]}")
    print(f"{INFO} 模型: {agent._model}")
    print(f"{INFO} 开始任务: '帮我查一下北京的天气'")
    try:
        result = await agent.run("帮我查一下北京的天气")
    except Exception as e:
        print(f"{FAIL} Agent.run 抛异常: {type(e).__name__}: {e}")
        traceback.print_exc()
        return 1
    finally:
        await agent.close()

    print()
    print(f"{INFO} 最终结果: {str(result)[:400]}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
