"""验证 DeepSeek 思考模式 reasoning_content 回传修复。

场景：模拟主对话/Agent 多轮工具调用的消息结构。
- 第 1 轮：请求模型，返回 assistant（可能带 reasoning_content + tool_calls）
- 第 2 轮：把 assistant 消息原样回传（含 reasoning_content）再请求
- 修复前：DeepSeek 返回 400 "The reasoning_content ... must be passed back"
- 修复后：正常响应

用法：python -u debug_reasoning_roundtrip.py
"""
import os, sys, asyncio, json
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


async def main() -> int:
    from ev.utils import config
    from ev.llm.client.factory import get_async_openai_client

    cfg = config.cfg
    model = cfg.LLM_MODEL or ""
    print(f"{INFO} 模型: {model}  服务: {cfg.LLM_BASE_URL}")

    client = get_async_openai_client(
        api_key=cfg.LLM_API_KEY, base_url=cfg.LLM_BASE_URL, timeout=60.0)

    messages = [
        {"role": "system", "content": "你是 E.V 虚拟主播助手。可用工具：get_weather(city)。"},
        {"role": "user", "content": "帮我查一下北京的天气"},
    ]
    tools = [{
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "查询指定城市天气",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
        },
    }]

    # ---- 第 1 轮：触发工具调用 ----
    print(f"\n{INFO} 第 1 轮：请求（期望返回 tool_calls + 可能带 reasoning_content）")
    resp = await client.chat.completions.create(
        model=model, messages=messages, tools=tools, tool_choice="auto")
    msg = resp.choices[0].message
    rc = getattr(msg, "reasoning_content", None) or ""
    content = (msg.content or "").strip()
    tcs = getattr(msg, "tool_calls", None) or []
    print(f"{INFO}   content={content[:100]!r}")
    print(f"{INFO}   reasoning_content_len={len(rc)}  tool_calls={len(tcs)}")
    if rc:
        print(f"{INFO}   reasoning 前 100 字: {rc[:100]!r}")

    if not tcs:
        print(f"{FAIL} 第 1 轮未返回 tool_calls（模型可能直接回答），无法验证多轮回传")
        return 2

    # ---- 构造 assistant 消息（含 reasoning_content，模拟修复后的代码） ----
    assistant_msg = {
        "role": "assistant",
        "content": content or None,
        "tool_calls": [
            {"id": tc.id, "type": "function",
             "function": {"name": tc.function.name,
                          "arguments": tc.function.arguments}}
            for tc in tcs
        ],
    }
    if rc:
        assistant_msg["reasoning_content"] = rc  # 修复点
        print(f"\n{INFO} 回传 assistant 消息带 reasoning_content ({len(rc)} 字)")
    else:
        print(f"\n{INFO} 模型未返回 reasoning_content（无回传需求）")

    messages.append(assistant_msg)
    # tool 响应
    messages.append({
        "role": "tool",
        "tool_call_id": tcs[0].id,
        "content": json.dumps({"city": "北京", "weather": "晴 25℃"}, ensure_ascii=False),
    })

    # ---- 第 2 轮：验证不 400 ----
    print(f"\n{INFO} 第 2 轮：带 reasoning_content 回传再请求")
    try:
        resp2 = await client.chat.completions.create(
            model=model, messages=messages, tools=tools, tool_choice="auto")
        msg2 = resp2.choices[0].message
        out = (msg2.content or "").strip()
        print(f"{PASS} 第 2 轮正常响应，content={out[:150]!r}")
        return 0
    except Exception as e:
        print(f"{FAIL} 第 2 轮异常: {type(e).__name__}: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
